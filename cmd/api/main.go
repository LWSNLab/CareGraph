// Command api is the CareGraph HTTP gateway — the single entry point of the
// modular monolith. It wires infrastructure (config, DB) into the domain
// modules (provider, search, auth) and serves the public REST API.
//
// See docs (LWSNLab/CareGraph_Doc): architecture/system-overview.md.
package main

import (
	"log"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/infrastructure"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/gin-gonic/gin"
)

// requestTimeout bounds a whole request. Generous next to a p95 of ~9 ms: it
// exists to stop a stalled dependency from pinning connections, not to police
// normal traffic.
const requestTimeout = 15 * time.Second

// Server-level limits. Without them a client can hold a connection open by
// trickling a request forever, and a slow reader can pin a response goroutine.
const (
	readHeaderTimeout = 5 * time.Second
	readTimeout       = 15 * time.Second
	writeTimeout      = 30 * time.Second
	idleTimeout       = 60 * time.Second
)

func main() {
	cfg := infrastructure.LoadConfig()

	// JSON to stderr: handlers log the cause of every 5xx here, and structured
	// output is what a log aggregator can filter on. The `service` field is set
	// once, so records stay attributable when several producers are collected
	// together. Level via CAREGRAPH_LOG_LEVEL.
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{
		Level: cfg.LogLevel,
	})).With("service", "caregraph-api"))

	pool, err := infrastructure.NewPostgresPool(cfg)
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	defer pool.Close()

	repo := provider.NewPostgresRepository(pool)
	handler := provider.NewHandler(repo)

	r := gin.New()

	// A known path with the wrong method is a 405, not a 404. Gin only
	// distinguishes the two when this is on, and fills in the Allow header.
	r.HandleMethodNotAllowed = true

	// Order matters: RequestID first so everything downstream — including the
	// recovery handler — can label its output with the correlation id.
	r.Use(
		httpx.RequestID(),
		httpx.Recovery(slog.Default()),
		gin.Logger(),
		httpx.Timeout(requestTimeout),
	)

	// Framework-generated failures answer in the same shape as the handlers.
	r.NoRoute(httpx.NoRoute())
	r.NoMethod(httpx.NoMethod())

	// Public: liveness / readiness probe.
	r.GET("/healthz", handler.Health)

	// Authenticated API surface (v1).
	v1 := r.Group("/v1")
	v1.Use(auth.APIKeyMiddleware(cfg))
	{
		v1.GET("/infrastructure/near", handler.Near)
		v1.GET("/infrastructure/search", handler.Search)
		v1.GET("/infrastructure/:ik_nummer", handler.GetByIK)
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           r,
		ReadHeaderTimeout: readHeaderTimeout,
		ReadTimeout:       readTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
	}

	slog.Info("CareGraph API listening", "addr", cfg.HTTPAddr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
