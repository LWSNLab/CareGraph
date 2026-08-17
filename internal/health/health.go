// Package health serves the two probes an orchestrator needs, which answer
// different questions.
//
// Liveness (/healthz) asks whether the process works. Failing it triggers a
// restart, so it must only fail for what a restart can fix — checking the
// database here would turn an outage into a restart loop across every replica.
//
// Readiness (/readyz) asks whether this instance should receive traffic. It may
// fail transiently; the orchestrator removes the instance and puts it back.
// Dependencies belong here, weighted by Severity.
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
	// Required: nothing works without it, so the instance must leave the load
	// balancer rather than serve 500s. Postgres.
	Required Severity = iota

	// Optional: the API still serves, with less — Redis (the limiter fails open,
	// so quotas stop being enforced) and Typesense (only /search is affected).
	// Removing the instance would cost capacity and fix nothing.
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

// A probe that hangs is indistinguishable from a dependency that is down, and
// the orchestrator is waiting.
const probeTimeout = 2 * time.Second

// /readyz is unauthenticated and queries every dependency, so without a cache it
// turns cheap HTTP requests into database round trips. A second is far below any
// sensible probe interval.
const cacheTTL = time.Second

// Checker runs the registered probes and serves both endpoints.
type Checker struct {
	deps []dependency
	log  *slog.Logger

	// Fields rather than the constants directly, so the package's own tests can
	// shrink them. Not settable from outside — these are not deployment knobs.
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

// Ready is GET /readyz: 503 when a Required dependency is down, 200 with
// "degraded" when only Optional ones are.
//
// The body reports a state per dependency but never the underlying error — a
// driver error carries the DSN, and this endpoint needs no credential. Causes go
// to the log, as they do for a 500.
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

			// A required failure wins outright.
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
