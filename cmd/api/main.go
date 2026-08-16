// Command api is the CareGraph HTTP gateway — the single entry point of the
// modular monolith. It wires infrastructure (config, DB) into the domain
// modules (provider, search, auth) and serves the public REST API.
//
// The route table itself lives in internal/httpapi, where it can be built
// without dependencies and compared against the OpenAPI contract in a test.
//
// See docs (LWSNLab/CareGraph_Doc): architecture/system-overview.md.
package main

import (
	"context"
	"log"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/LWSNLab/caregraph/internal/auth"
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

	r, err := httpapi.NewRouter(httpapi.Deps{
		Provider: handler,
		Keys:     keys,
		Limiter:  limiter,
		Log:      slog.Default(),
	})
	if err != nil {
		log.Fatalf("router: %v", err)
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
