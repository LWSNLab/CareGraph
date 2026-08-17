// Command api is the CareGraph HTTP gateway — the single entry point of the
// modular monolith. It wires infrastructure (config, DB) into the domain
// modules (provider, search, auth) and serves the public REST API.
//
// The route table itself lives in internal/httpapi, where it can be built
// without dependencies and compared against the OpenAPI contract in a test.
//
// Run with -healthcheck it probes a running instance instead of starting one;
// see runHealthcheck.
//
// See docs (LWSNLab/CareGraph_Doc): architecture/system-overview.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/LWSNLab/caregraph/internal/health"
	"github.com/LWSNLab/caregraph/internal/httpapi"
	"github.com/LWSNLab/caregraph/internal/infrastructure"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/LWSNLab/caregraph/internal/ratelimit"
	"github.com/LWSNLab/caregraph/internal/search"
	"github.com/redis/go-redis/v9"
)

// Server-level limits. Without them a client can hold a connection open by
// trickling a request forever, and a slow reader can pin a response goroutine.
const (
	readHeaderTimeout = 5 * time.Second
	readTimeout       = 15 * time.Second
	writeTimeout      = 30 * time.Second
	idleTimeout       = 60 * time.Second
)

// shutdownTimeout bounds how long a graceful stop waits for in-flight requests.
//
// Comfortably above httpapi.DefaultRequestTimeout (15 s), which already caps any
// single request: a handler cannot outlive that, so this is long enough for every
// in-flight request to finish rather than being cut off mid-response. The
// compose service sets stop_grace_period above it, otherwise Docker would send
// SIGKILL while the drain is still running and the point would be lost.
const shutdownTimeout = 20 * time.Second

// healthcheckTimeout bounds the -healthcheck probe. A container healthcheck has
// its own timeout; this only stops the process hanging if the socket accepts and
// then goes quiet.
const healthcheckTimeout = 3 * time.Second

func main() {
	probe := flag.Bool("healthcheck", false,
		"probe a running instance's /readyz and exit 0 when it is ready")
	flag.Parse()

	if *probe {
		os.Exit(runHealthcheck(os.Getenv("CAREGRAPH_HTTP_ADDR")))
	}

	// run rather than inlining here: log.Fatal calls os.Exit, which skips defers,
	// so a fatal path would leak the pool and the Redis client. With an exit code
	// returned instead, every defer runs — which is what makes the graceful
	// shutdown below actually complete.
	os.Exit(run())
}

func run() int {
	cfg, err := infrastructure.LoadConfig()
	if err != nil {
		// Before the logger is configured, so this goes to stderr plainly. It is a
		// startup misconfiguration, not an operational event to aggregate.
		fmt.Fprintf(os.Stderr, "config: %v\n", err)
		return 1
	}

	// JSON to stderr: handlers log the cause of every 5xx here, and structured
	// output is what a log aggregator can filter on. The `service` field is set
	// once, so records stay attributable when several producers are collected
	// together. Level via CAREGRAPH_LOG_LEVEL.
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{
		Level: cfg.LogLevel,
	})).With("service", "caregraph-api"))

	pool, err := infrastructure.NewPostgresPool(cfg)
	if err != nil {
		slog.Error("postgres unreachable at startup", "error", err)
		return 1
	}
	defer pool.Close()

	redisClient := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer redisClient.Close()
	limiter := ratelimit.New(redisClient)

	// Reachability is reported once at startup rather than left to the first
	// request: the limiter fails open, so an unreachable Redis is otherwise
	// invisible until someone notices the quotas are not being applied.
	if err := limiter.Ping(context.Background()); err != nil {
		slog.Warn("redis unreachable — rate limits will not be enforced", "error", err)
	}

	repo := provider.NewPostgresRepository(pool)
	keys := auth.NewPostgresKeyStore(pool)

	searchClient := search.NewTypesenseClient(cfg.TypesenseURL, cfg.TypesenseKey)
	handler := provider.NewHandler(repo).WithSearch(searchClient)

	// Reported once at startup for the same reason as Redis: /search answers 503
	// when the engine is down, and an operator should learn that from a log line
	// rather than from the first user who tries to search.
	if err := searchClient.Ping(context.Background()); err != nil {
		slog.Warn("search engine unreachable — /search will answer 503", "error", err)
	}

	// Severity per dependency mirrors how the API actually degrades: without
	// Postgres nothing works, without Redis quotas stop being enforced, without
	// Typesense only /search is affected. See internal/health.
	checker := health.New(slog.Default()).
		Register("postgres", health.Required, pool.Ping).
		Register("redis", health.Optional, limiter.Ping).
		Register("search", health.Optional, searchClient.Ping)

	r, err := httpapi.NewRouter(httpapi.Deps{
		Provider: handler,
		Health:   checker,
		Keys:     keys,
		Limiter:  limiter,
		Log:      slog.Default(),
	})
	if err != nil {
		slog.Error("building the router failed", "error", err)
		return 1
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           r,
		ReadHeaderTimeout: readHeaderTimeout,
		ReadTimeout:       readTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
	}

	// SIGTERM is what an orchestrator sends on deploy, scale-down and rollback —
	// the routine cases, not the exceptional ones. Without this the process dies
	// mid-response and the caller sees a connection reset that looks like a bug in
	// the API.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	listenErr := make(chan error, 1)
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			listenErr <- err
			return
		}
		listenErr <- nil
	}()

	slog.Info("CareGraph API listening", "addr", cfg.HTTPAddr)

	select {
	case err := <-listenErr:
		if err != nil {
			slog.Error("server stopped", "error", err)
			return 1
		}
		return 0

	case <-ctx.Done():
		// Restore the default handler: a second signal from an impatient operator
		// should kill the process rather than be swallowed by this one.
		stop()
		slog.Info("shutting down, draining in-flight requests",
			"timeout", shutdownTimeout.String())
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		// The deadline passed with requests still running. Worth an error: it means
		// either a handler outlived its own timeout or the grace period is too short.
		slog.Error("graceful shutdown did not finish in time", "error", err)
		return 1
	}

	slog.Info("shutdown complete")
	return 0
}

// runHealthcheck probes a running instance over HTTP and returns an exit code.
//
// It exists because the runtime image is distroless: there is no shell, no curl
// and no wget for a container healthcheck to invoke, and this binary is the only
// executable in the image. `HEALTHCHECK ["/api", "-healthcheck"]` therefore has
// to be served by the binary itself.
//
// It probes /readyz rather than /healthz. A compose healthcheck gates
// `depends_on: service_healthy`, and what a dependent service needs to know is
// "can this serve traffic", not "is the process alive" — which is the same
// distinction the two endpoints exist for.
func runHealthcheck(addr string) int {
	if addr == "" {
		addr = ":8080"
	}

	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: cannot read address %q: %v\n", addr, err)
		return 1
	}
	// A wildcard listen address is not a destination; probe the loopback instead.
	switch host {
	case "", "0.0.0.0", "::", "[::]":
		host = "127.0.0.1"
	}

	client := &http.Client{Timeout: healthcheckTimeout}
	url := "http://" + net.JoinHostPort(host, port) + "/readyz"

	resp, err := client.Get(url)
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %v\n", err)
		return 1
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "healthcheck: %s returned %d\n", url, resp.StatusCode)
		return 1
	}
	return 0
}
