// Package httpapi assembles the HTTP route table.
//
// Separate from cmd/api so the table can be built without a database, Redis or a
// search engine: api/spec_test.go constructs the real router with stub
// dependencies and compares it against the OpenAPI document, so the comparison
// covers the routes actually served rather than a hand-kept second list.
package httpapi

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/LWSNLab/caregraph/api"
	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/LWSNLab/caregraph/internal/health"
	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/LWSNLab/caregraph/internal/ratelimit"
	"github.com/gin-gonic/gin"
)

// DefaultRequestTimeout bounds a whole request. Generous next to a p95 of ~9 ms:
// it exists to stop a stalled dependency from pinning connections.
const DefaultRequestTimeout = 15 * time.Second

// Deps are the collaborators the routes need.
type Deps struct {
	Provider *provider.Handler
	Health   *health.Checker
	Keys     auth.KeyStore
	Limiter  *ratelimit.Limiter
	Log      *slog.Logger

	// TrustedProxies are the addresses whose X-Forwarded-For may be believed.
	// Empty trusts none — see the comment at SetTrustedProxies below.
	TrustedProxies []string

	// RequestTimeout defaults to DefaultRequestTimeout when zero.
	RequestTimeout time.Duration
}

// NewRouter builds the route table. Its only error comes from SetTrustedProxies,
// which is a configuration mistake and should be treated as fatal.
func NewRouter(d Deps) (*gin.Engine, error) {
	log := d.Log
	if log == nil {
		log = slog.Default()
	}
	timeout := d.RequestTimeout
	if timeout == 0 {
		timeout = DefaultRequestTimeout
	}

	r := gin.New()

	// A known path with the wrong method is a 405, not a 404. Gin only
	// distinguishes the two when this is on, and fills in the Allow header.
	r.HandleMethodNotAllowed = true

	// Both directions of this are a real failure, which is why it is explicit.
	//
	// Trusting everyone lets a client forge X-Forwarded-For and walk around the
	// per-address failed-auth budget. Trusting nobody while a proxy *is* in front
	// makes ClientIP() report the proxy for every request, so every client shares
	// one rate-limit bucket and one failed-auth budget — one bad client would lock
	// out all of them.
	//
	// So: empty behind nothing, and exactly the proxy's address behind one.
	if err := r.SetTrustedProxies(d.TrustedProxies); err != nil {
		return nil, err
	}

	// Order matters: RequestID first so everything downstream — including the
	// recovery handler — can label its output with the correlation id.
	r.Use(
		httpx.RequestID(),
		httpx.Recovery(log),
		httpx.AccessLog(log),
		httpx.Timeout(timeout),
		auth.SecurityHeaders(),
	)

	// Framework-generated failures answer in the same shape as the handlers.
	r.NoRoute(httpx.NoRoute())
	r.NoMethod(httpx.NoMethod())

	// Unauthenticated: a probe that needs a credential is one more thing for an
	// orchestrator to get wrong, and a contract you need a key to read cannot tell
	// you whether to ask for one.
	r.GET("/healthz", d.Health.Live)
	r.GET("/readyz", d.Health.Ready)
	r.GET("/openapi.yaml", ServeSpec)

	// Order is security-relevant. GuardAuthAttempts runs first and is *not* a
	// quota: it only checks whether this client has burned its failed-auth budget,
	// so a stream of wrong secrets cannot keep Argon2id busy. It charges nothing on
	// success — an earlier version enforced the community rate here and silently
	// throttled enterprise keys to 100/min. The tier quota runs last, because the
	// tier is only known once the key is.
	v1 := r.Group("/v1")
	v1.Use(
		auth.GuardAuthAttempts(d.Limiter, log),
		auth.APIKeyMiddleware(d.Keys, d.Limiter, log),
		auth.RateLimitByKey(d.Limiter, log),
	)
	{
		v1.GET("/infrastructure/near", d.Provider.Near)
		v1.GET("/infrastructure/search", d.Provider.Search)
		v1.GET("/infrastructure/:ik_nummer", d.Provider.GetByIK)
	}

	return r, nil
}

// ServeSpec returns the contract this binary implements.
func ServeSpec(c *gin.Context) {
	c.Data(http.StatusOK, api.SpecContentType, api.SpecYAML)
}
