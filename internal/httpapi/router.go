// Package httpapi assembles the HTTP route table.
//
// It is separate from cmd/api so that the route table can be built without a
// database, a Redis or a search engine: api/spec_test.go constructs the real
// router with zero-valued dependencies and compares it against the OpenAPI
// document. Route registration never touches those dependencies — the
// middleware closures capture them but are not invoked — so the comparison
// tests the routes that are actually served, not a hand-kept second list.
package httpapi

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/LWSNLab/caregraph/api"
	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/LWSNLab/caregraph/internal/ratelimit"
	"github.com/gin-gonic/gin"
)

// DefaultRequestTimeout bounds a whole request. Generous next to a p95 of ~9 ms:
// it exists to stop a stalled dependency from pinning connections, not to police
// normal traffic.
const DefaultRequestTimeout = 15 * time.Second

// Deps are the collaborators the routes need.
type Deps struct {
	Provider *provider.Handler
	Keys     auth.KeyStore
	Limiter  *ratelimit.Limiter
	Log      *slog.Logger

	// RequestTimeout defaults to DefaultRequestTimeout when zero.
	RequestTimeout time.Duration
}

// NewRouter builds the route table.
//
// The only error it can return comes from SetTrustedProxies, which is a
// configuration mistake rather than a runtime condition — the caller should
// treat it as fatal.
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

	// Trust no proxy headers by default: ClientIP() then reports the peer that
	// actually connected. With X-Forwarded-For trusted, anyone could spoof it and
	// walk around the per-client failed-auth budget. A deployment behind a load
	// balancer must set this to that balancer's address, and only that.
	if err := r.SetTrustedProxies(nil); err != nil {
		return nil, err
	}

	// Order matters: RequestID first so everything downstream — including the
	// recovery handler — can label its output with the correlation id.
	r.Use(
		httpx.RequestID(),
		httpx.Recovery(log),
		gin.Logger(),
		httpx.Timeout(timeout),
		auth.SecurityHeaders(),
	)

	// Framework-generated failures answer in the same shape as the handlers.
	r.NoRoute(httpx.NoRoute())
	r.NoMethod(httpx.NoMethod())

	// Unauthenticated. A liveness probe that needs a credential is one more thing
	// to get wrong in an orchestrator, and a contract you need a key to read
	// cannot be used to decide whether to ask for a key.
	r.GET("/healthz", d.Provider.Health)
	r.GET("/openapi.yaml", ServeSpec)

	// Authenticated API surface (v1).
	//
	// Order is security-relevant.
	//
	// GuardAuthAttempts runs first and is *not* a quota: it checks whether this
	// client has already exhausted its failed-authentication budget, so a stream
	// of wrong secrets cannot keep Argon2id busy. It charges nothing on success,
	// which is why it cannot cap a legitimate key — an earlier version enforced
	// the community rate here and silently throttled enterprise keys to 100/min.
	//
	// The tier quota runs last, because the tier is only known once the key is.
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
