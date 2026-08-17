// Package health serves the two probes an orchestrator needs, which are not the
// same question.
//
// **Liveness** asks whether the process is working. A failing liveness probe
// makes Kubernetes restart the container, so it must only fail for conditions a
// restart can fix — a deadlock, a corrupted process. If it also checked the
// database, a database outage would restart every replica in a loop, turning a
// recoverable dependency failure into a total one. `/healthz` therefore answers
// as long as the process can answer, which is the whole of what it claims.
//
// **Readiness** asks whether this instance should receive traffic right now. It
// may fail transiently; the orchestrator takes the pod out of the load balancer
// and puts it back when it recovers. That is where dependencies belong, and it
// is what `/readyz` reports.
//
// Which dependency counts is decided by how the API actually behaves without it,
// not by how important it sounds — see Severity.
package health

import (
	"context"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/gin-gonic/gin"
)

// Severity says what the absence of a dependency means for serving traffic.
type Severity int

const (
	// Required: without it the API cannot answer at all. Postgres — every
	// endpoint reads from it, so an instance without it must leave the load
	// balancer rather than serve 500s.
	Required Severity = iota

	// Optional: the API still serves, with less. Redis — the rate limiter fails
	// open by design, so quotas stop being enforced but requests succeed.
	// Typesense — /search answers 503 while /near and the IK lookup are
	// unaffected. Pulling an instance out of rotation for either would remove
	// working capacity to fix nothing.
	Optional
)

// Probe is one dependency check. It must respect the context deadline.
type Probe func(ctx context.Context) error

type dependency struct {
	name     string
	severity Severity
	probe    Probe
}

// Status values reported per dependency and for the instance as a whole.
const (
	StatusOK          = "ok"
	StatusDegraded    = "degraded"
	StatusUnavailable = "unavailable"
)

// probeTimeout bounds each dependency check. Short: a probe that hangs is
// indistinguishable from a dependency that is down, and the orchestrator is
// waiting.
const probeTimeout = 2 * time.Second

// cacheTTL is how long a readiness result is reused.
//
// /readyz is unauthenticated and issues a query per dependency, which makes it
// an amplification vector: without this, anyone could turn a flood of cheap HTTP
// requests into a flood of database round trips. One second is far below any
// sensible probe interval, so an orchestrator never sees a stale answer.
const cacheTTL = time.Second

// Checker runs the registered probes and serves both endpoints.
type Checker struct {
	deps []dependency
	log  *slog.Logger

	// ttl and timeout are fields rather than the constants directly so the
	// package's own tests can shrink them. Nothing outside can set them: the
	// values are a property of the design, not a deployment knob.
	ttl     time.Duration
	timeout time.Duration

	mu       sync.Mutex
	cached   response
	cachedAt time.Time
}

// New builds a Checker logging to log.
func New(log *slog.Logger) *Checker {
	if log == nil {
		log = slog.Default()
	}
	return &Checker{log: log, ttl: cacheTTL, timeout: probeTimeout}
}

// Register adds a dependency. Order is preserved in the response.
func (c *Checker) Register(name string, severity Severity, probe Probe) *Checker {
	c.deps = append(c.deps, dependency{name: name, severity: severity, probe: probe})
	return c
}

type checkResult struct {
	Status    string  `json:"status"`
	LatencyMs float64 `json:"latency_ms"`
}

type response struct {
	Status string                 `json:"status"`
	Checks map[string]checkResult `json:"checks"`
}

// Live is GET /healthz. It answers if the process can answer.
func (c *Checker) Live(ctx *gin.Context) {
	ctx.JSON(http.StatusOK, gin.H{"status": StatusOK})
}

// Ready is GET /readyz.
//
// 503 when a Required dependency is down — this instance cannot serve, take it
// out of rotation. 200 with status "degraded" when only Optional ones are: the
// instance still answers most requests, and removing it would cost capacity
// without fixing anything.
//
// The body names each dependency and its state but never the underlying error.
// A driver error carries the DSN, the query and column names; this endpoint is
// unauthenticated, so the cause goes to the log under the request id, exactly as
// it does for a 500.
func (c *Checker) Ready(ctx *gin.Context) {
	result := c.evaluate(ctx)

	status := http.StatusOK
	if result.Status == StatusUnavailable {
		status = http.StatusServiceUnavailable
	}
	ctx.JSON(status, result)
}

// evaluate returns a fresh result, or the cached one if it is still warm.
func (c *Checker) evaluate(ctx *gin.Context) response {
	c.mu.Lock()
	defer c.mu.Unlock()

	if time.Since(c.cachedAt) < c.ttl && c.cached.Checks != nil {
		return c.cached
	}

	result := response{Status: StatusOK, Checks: make(map[string]checkResult, len(c.deps))}

	for _, dep := range c.deps {
		probeCtx, cancel := context.WithTimeout(ctx.Request.Context(), c.timeout)
		started := time.Now()
		err := dep.probe(probeCtx)
		latency := time.Since(started)
		cancel()

		state := StatusOK
		if err != nil {
			state = StatusUnavailable

			httpx.Logger(c.log, ctx).ErrorContext(ctx.Request.Context(),
				"readiness probe failed",
				"dependency", dep.name,
				"required", dep.severity == Required,
				"error", err)

			// A required failure wins outright; an optional one only downgrades
			// a result that is still otherwise healthy.
			if dep.severity == Required {
				result.Status = StatusUnavailable
			} else if result.Status == StatusOK {
				result.Status = StatusDegraded
			}
		}

		result.Checks[dep.name] = checkResult{
			Status:    state,
			LatencyMs: float64(latency.Microseconds()) / 1000,
		}
	}

	c.cached, c.cachedAt = result, time.Now()
	return result
}
