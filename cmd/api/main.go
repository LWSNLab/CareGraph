// Command api is the CareGraph HTTP gateway — the single entry point of the
// modular monolith. It wires infrastructure (config, DB) into the domain
// modules (provider, search, auth) and serves the public REST API.
//
// See docs (LWSNLab/CareGraph_Doc): architecture/system-overview.md.
package main

import (
	"log"

	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/LWSNLab/caregraph/internal/infrastructure"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/gin-gonic/gin"
)

func main() {
	cfg := infrastructure.LoadConfig()

	pool, err := infrastructure.NewPostgresPool(cfg)
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	defer pool.Close()

	repo := provider.NewPostgresRepository(pool)
	handler := provider.NewHandler(repo)

	r := gin.New()
	r.Use(gin.Logger(), gin.Recovery())

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

	log.Printf("CareGraph API listening on %s", cfg.HTTPAddr)
	if err := r.Run(cfg.HTTPAddr); err != nil {
		log.Fatal(err)
	}
}
