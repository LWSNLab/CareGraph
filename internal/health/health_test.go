package health

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func serve(t *testing.T, c *Checker) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/readyz", c.Ready)

	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) response {
	t.Helper()
	var body response
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v (%s)", err, rec.Body)
	}
	return body
}

func ok(context.Context) error   { return nil }
func down(context.Context) error { return errors.New("connection refused: dial tcp 10.0.0.5:5432") }

// TestSeverityDecidesTheStatusCode is the whole point of the endpoint: an
// optional dependency going down must not take the instance out of the load
// balancer, because that removes working capacity without fixing anything.
func TestSeverityDecidesTheStatusCode(t *testing.T) {
	cases := []struct {
		name       string
		postgres   Probe
		redis      Probe
		wantStatus int
		wantBody   string
	}{
		{"all up", ok, ok, http.StatusOK, StatusOK},
		{"optional down", ok, down, http.StatusOK, StatusDegraded},
		{"required down", down, ok, http.StatusServiceUnavailable, StatusUnavailable},
		{"both down", down, down, http.StatusServiceUnavailable, StatusUnavailable},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := New(quiet()).
				Register("postgres", Required, tc.postgres).
				Register("redis", Optional, tc.redis)

			rec := serve(t, c)
			if rec.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, tc.wantStatus)
			}
			if got := decode(t, rec).Status; got != tc.wantBody {
				t.Errorf("body status = %q, want %q", got, tc.wantBody)
			}
		})
	}
}

// TestProbeErrorsNeverReachTheBody guards an unauthenticated endpoint against
// leaking what a driver puts in its error text — here a host and port, in
// practice a full DSN. The cause belongs in the log, as it does for a 500.
func TestProbeErrorsNeverReachTheBody(t *testing.T) {
	rec := serve(t, New(quiet()).Register("postgres", Required, down))

	body := rec.Body.String()
	for _, leaked := range []string{"10.0.0.5", "5432", "connection refused", "dial tcp"} {
		if strings.Contains(body, leaked) {
			t.Errorf("response leaks %q from the driver error: %s", leaked, body)
		}
	}
	if !strings.Contains(body, StatusUnavailable) {
		t.Errorf("response should still report the dependency as unavailable: %s", body)
	}
}

// TestResultIsCached matters because /readyz needs no credential and issues one
// query per dependency. Without the cache, cheap HTTP requests amplify into
// database round trips.
func TestResultIsCached(t *testing.T) {
	var calls atomic.Int64
	c := New(quiet()).Register("postgres", Required, func(context.Context) error {
		calls.Add(1)
		return nil
	})

	for range 20 {
		serve(t, c)
	}
	if got := calls.Load(); got != 1 {
		t.Errorf("probe ran %d times for 20 requests, want 1", got)
	}
}

// TestCacheExpires — the other half. A cache that never expired would report a
// dead database as healthy for as long as the process lived.
func TestCacheExpires(t *testing.T) {
	var calls atomic.Int64
	c := New(quiet()).Register("postgres", Required, func(context.Context) error {
		calls.Add(1)
		return nil
	})
	c.ttl = 10 * time.Millisecond

	serve(t, c)
	time.Sleep(20 * time.Millisecond)
	serve(t, c)

	if got := calls.Load(); got != 2 {
		t.Errorf("probe ran %d times across the TTL boundary, want 2", got)
	}
}

// TestProbeTimeoutIsEnforced: a probe that hangs is indistinguishable from a
// dependency that is down, and the orchestrator is waiting for an answer.
func TestProbeTimeoutIsEnforced(t *testing.T) {
	c := New(quiet()).Register("postgres", Required, func(ctx context.Context) error {
		<-ctx.Done() // never returns on its own
		return ctx.Err()
	})
	c.timeout = 20 * time.Millisecond

	started := time.Now()
	rec := serve(t, c)
	elapsed := time.Since(started)

	if elapsed > time.Second {
		t.Errorf("probe took %v; the timeout did not apply", elapsed)
	}
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want 503 — a hung probe is a failed probe", rec.Code)
	}
}

// TestLiveIgnoresDependencies pins the distinction the two endpoints exist for.
// If liveness failed on a database outage, the orchestrator would restart every
// replica in a loop — a restart cannot fix someone else's database.
func TestLiveIgnoresDependencies(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/healthz", New(quiet()).Register("postgres", Required, down).Live)

	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 — liveness must not depend on Postgres", rec.Code)
	}
}
