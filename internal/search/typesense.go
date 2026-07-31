// Package search is the Go *client* for the search engine.
//
// The engine itself is Typesense — an in-memory, typo-tolerant search server
// written in C++ — which runs as its own `typesense` service (see
// docker-compose.yml), NOT as Go code. This package only speaks to it over
// HTTP from the Go gateway. See docs/architecture/system-overview.md
// ("C++ / Typesense") and roadmap Phase 2.2.
package search

import "context"

// Client is the search port used by the provider domain.
type Client interface {
	// Search performs a typo-tolerant query, optionally filtered by city,
	// and returns matching provider IDs.
	Search(ctx context.Context, query, city string) ([]string, error)
}

// TODO: implement a Typesense-backed Client plus a Postgres → Typesense sync
// worker that keeps the in-memory index up to date.
