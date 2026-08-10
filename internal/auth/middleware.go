// Package auth provides API-key authentication and rate limiting for the public
// gateway. See docs/architecture/security.md §2.
package auth

import (
	"errors"
	"log/slog"
	"net/http"
	"strconv"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/ratelimit"
	"github.com/gin-gonic/gin"
)

// contextKeyIdentity holds the verified identity for downstream handlers.
const contextKeyIdentity = "caregraph.identity"

// HeaderAPIKey is the credential header.
const HeaderAPIKey = "X-API-Key"

// Rate-limit headers, the widely implemented draft spelling.
const (
	headerLimit      = "X-RateLimit-Limit"
	headerRemaining  = "X-RateLimit-Remaining"
	headerRetryAfter = "Retry-After"
)

// APIKeyMiddleware verifies X-API-Key against the store.
//
// Both "no such key id" and "wrong secret" answer the same 401 with the same
// message. Distinguishing them would tell an attacker which key ids exist, which
// turns one unknown into two smaller ones.
func APIKeyMiddleware(store KeyStore, limiter *ratelimit.Limiter, log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		presented := c.GetHeader(HeaderAPIKey)
		if presented == "" {
			chargeFailedAuth(c, limiter, log)
			httpx.Abort(c, http.StatusUnauthorized, httpx.CodeUnauthorized,
				"Unauthorized: Invalid API Key provided")
			return
		}

		identity, err := store.Verify(c.Request.Context(), presented)
		switch {
		case err == nil:
			c.Set(contextKeyIdentity, identity)
			c.Next()

		case errors.Is(err, ErrMalformedKey), errors.Is(err, ErrNoSuchKey):
			// Expected traffic, not an incident: log at DEBUG so a scan does not
			// fill the error log. The key itself is never logged.
			httpx.Logger(log, c).DebugContext(c.Request.Context(),
				"api key rejected", "reason", err)
			chargeFailedAuth(c, limiter, log)
			httpx.Abort(c, http.StatusUnauthorized, httpx.CodeUnauthorized,
				"Unauthorized: Invalid API Key provided")

		default:
			// The store itself failed — a database outage, an unreadable hash.
			// That is ours, and it must not read as the client's fault.
			httpx.Logger(log, c).ErrorContext(c.Request.Context(),
				"api key verification failed", "error", err)
			httpx.Abort(c, http.StatusInternalServerError, httpx.CodeInternal,
				"internal error")
		}
	}
}

// chargeFailedAuth spends one token from the client's failed-auth budget.
//
// A failure to record is logged and otherwise ignored: the response is already
// decided, and a Redis outage must not turn a 401 into a 500.
func chargeFailedAuth(c *gin.Context, limiter *ratelimit.Limiter, log *slog.Logger) {
	if limiter == nil {
		return
	}
	if err := limiter.Spend(c.Request.Context(),
		failedAuthBucket(c), FailedAuthBudgetPerMin); err != nil {
		httpx.Logger(log, c).ErrorContext(c.Request.Context(),
			"could not record a failed authentication", "error", err)
	}
}

// IdentityFrom returns the verified identity, or nil before authentication.
func IdentityFrom(c *gin.Context) *Identity {
	if v, ok := c.Get(contextKeyIdentity); ok {
		if identity, ok := v.(*Identity); ok {
			return identity
		}
	}
	return nil
}

// FailedAuthBudgetPerMin caps how often one client address may fail
// authentication per minute.
//
// This is not a quota — it is the brake on Argon2id. Verification costs tens of
// milliseconds by design, so anyone who learns a valid key id could saturate a
// core by replaying it with varying wrong secrets. The store's negative cache
// stops a repeat of the *same* wrong secret; it cannot stop a stream of new ones.
//
// Deliberately charged only on failure. An earlier version limited *all* requests
// per IP at the community rate before authentication, which silently capped an
// enterprise key at 100 req/min instead of its 6000 — the pre-auth check cannot
// know the tier yet, so it must not enforce a quota. Legitimate traffic never
// touches this budget.
const FailedAuthBudgetPerMin = 20

// GuardAuthAttempts rejects a client that has already burned its failed-auth
// budget, before the expensive verification runs.
func GuardAuthAttempts(limiter *ratelimit.Limiter, log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		decision, err := limiter.Peek(c.Request.Context(),
			failedAuthBucket(c), FailedAuthBudgetPerMin)
		if err != nil {
			// Fails open, like the quota limiter — see enforce.
			httpx.Logger(log, c).ErrorContext(c.Request.Context(),
				"failed-auth guard unavailable, allowing request", "error", err)
			c.Next()
			return
		}
		if !decision.Allowed {
			seconds := max(int(decision.RetryAfter.Seconds()), 1)
			c.Header(headerRetryAfter, strconv.Itoa(seconds))
			httpx.Logger(log, c).WarnContext(c.Request.Context(),
				"too many failed authentications", "client_ip", c.ClientIP())
			httpx.Abort(c, http.StatusTooManyRequests, httpx.CodeRateLimited,
				"Too many failed authentication attempts. Try again in "+
					strconv.Itoa(seconds)+" seconds.")
			return
		}
		c.Next()
	}
}

func failedAuthBucket(c *gin.Context) string {
	return "authfail:" + c.ClientIP()
}

// RateLimitByKey throttles per API key according to its tier, after authentication.
func RateLimitByKey(limiter *ratelimit.Limiter, log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		identity := IdentityFrom(c)
		if identity == nil {
			// Only reachable if the middleware order is wrong. Fail closed
			// rather than serve an unlimited request.
			httpx.Logger(log, c).ErrorContext(c.Request.Context(),
				"rate limit by key ran before authentication")
			httpx.Abort(c, http.StatusInternalServerError, httpx.CodeInternal, "internal error")
			return
		}
		enforce(c, limiter, "key:"+identity.KeyID, identity.LimitPerMin, log)
	}
}

// enforce applies one bucket and either continues the chain or answers 429.
//
// **Fails open.** If Redis cannot be reached the request proceeds, with an error
// logged. The limiter exists to curb abuse; making it a hard dependency of every
// request would turn a cache outage into a total outage, which is the larger
// harm. Deliberate, and the reason the failure is logged at ERROR rather than
// swallowed.
func enforce(
	c *gin.Context, limiter *ratelimit.Limiter, bucket string, perMinute int, log *slog.Logger,
) {
	decision, err := limiter.Allow(c.Request.Context(), bucket, perMinute)
	if err != nil {
		httpx.Logger(log, c).ErrorContext(c.Request.Context(),
			"rate limiter unavailable, allowing request", "bucket", bucket, "error", err)
		c.Next()
		return
	}

	c.Header(headerLimit, strconv.Itoa(perMinute))
	c.Header(headerRemaining, strconv.Itoa(decision.Remaining))

	if !decision.Allowed {
		seconds := int(decision.RetryAfter.Seconds())
		if seconds < 1 {
			seconds = 1
		}
		c.Header(headerRetryAfter, strconv.Itoa(seconds))
		httpx.Abort(c, http.StatusTooManyRequests, httpx.CodeRateLimited,
			"Rate limit exceeded. Try again in "+strconv.Itoa(seconds)+" seconds.")
		return
	}

	c.Next()
}

// SecurityHeaders sets the response headers from docs/architecture/security.md §2.
func SecurityHeaders() gin.HandlerFunc {
	return func(c *gin.Context) {
		// A JSON API should never be interpreted as anything else.
		c.Header("X-Content-Type-Options", "nosniff")
		// Nothing in an API response may load or execute anything.
		c.Header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
		c.Header("Referrer-Policy", "no-referrer")
		// Only meaningful over TLS, and asserting it on a plaintext request would
		// be ignored anyway — set it when the request actually arrived encrypted.
		if c.Request.TLS != nil || c.GetHeader("X-Forwarded-Proto") == "https" {
			c.Header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
		}
		c.Next()
	}
}
