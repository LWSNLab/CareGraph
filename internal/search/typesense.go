// Package search is the Go *client* for the search engine.
//
// The engine itself is Typesense — an in-memory, typo-tolerant search server
// written in C++ — which runs as its own `typesense` service (see
// docker-compose.yml), NOT as Go code. This package only speaks to it over HTTP
// from the Go gateway. The index is filled by the Python sync worker (E2-S2);
// nothing here writes to it.
package search

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// ErrUnavailable means the engine could not be reached or refused the request.
// Distinct from "no results": the caller answers 503, because search is
// temporarily impossible rather than empty.
var ErrUnavailable = errors.New("search engine unavailable")

// Alias the sync worker publishes. Queries go through it rather than a
// collection name, so a rebuild can swap the underlying index atomically.
const alias = "providers"

// Field weights. A match on the name is what the user meant; a match on the
// street is usually incidental.
const (
	queryFields  = "name,ort,parent_organization,strasse"
	queryWeights = "8,4,2,1"
)

// Query is one search request.
type Query struct {
	Text  string
	City  string
	Type  string
	Limit int
}

// Result is the engine's answer: ranked identifiers plus the total match count.
//
// Only identifiers, deliberately. The index holds a subset of the fields — no
// IK, no website, no details — so returning its documents would give `/search` a
// different response shape from `/near`. The caller hydrates from Postgres, the
// source of truth, and both endpoints answer with the same schema.
type Result struct {
	IDs   []string
	Found int
}

// Client is the search port used by the provider domain.
type Client interface {
	Search(ctx context.Context, q Query) (*Result, error)
	Ping(ctx context.Context) error
}

// TypesenseClient talks to a Typesense server over HTTP.
type TypesenseClient struct {
	baseURL string
	apiKey  string
	http    *http.Client
}

// NewTypesenseClient wires a client against the given server.
func NewTypesenseClient(baseURL, apiKey string) *TypesenseClient {
	return &TypesenseClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		// Bounded independently of the request context: a slow engine must not
		// be able to hold a gateway worker for the whole request budget.
		http: &http.Client{Timeout: 5 * time.Second},
	}
}

type searchResponse struct {
	Found int `json:"found"`
	Hits  []struct {
		Document struct {
			ID string `json:"id"`
		} `json:"document"`
	} `json:"hits"`
}

// Search returns matching identifiers, most relevant first.
func (c *TypesenseClient) Search(ctx context.Context, q Query) (*Result, error) {
	params := url.Values{}
	params.Set("q", q.Text)
	params.Set("query_by", queryFields)
	params.Set("query_by_weights", queryWeights)
	params.Set("per_page", strconv.Itoa(q.Limit))
	// Only the id is needed; the rest of the document is fetched from Postgres.
	params.Set("include_fields", "id")

	if filters := buildFilters(q); filters != "" {
		params.Set("filter_by", filters)
	}

	endpoint := fmt.Sprintf("%s/collections/%s/documents/search?%s",
		c.baseURL, alias, params.Encode())

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("build search request: %w", err)
	}
	req.Header.Set("X-TYPESENSE-API-KEY", c.apiKey)

	resp, err := c.http.Do(req)
	if err != nil {
		// Includes a cancelled or expired context; the handler tells those apart
		// from a genuinely unreachable engine by inspecting the wrapped error.
		return nil, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// 404 here means the alias does not exist — the sync worker has never
		// run. That is an operational gap, not a client mistake.
		return nil, fmt.Errorf("%w: search returned %s", ErrUnavailable, resp.Status)
	}

	var body searchResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("%w: decode search response: %w", ErrUnavailable, err)
	}

	ids := make([]string, 0, len(body.Hits))
	for _, hit := range body.Hits {
		if hit.Document.ID != "" {
			ids = append(ids, hit.Document.ID)
		}
	}
	return &Result{IDs: ids, Found: body.Found}, nil
}

// buildFilters turns the optional narrowing into Typesense filter syntax.
func buildFilters(q Query) string {
	var filters []string
	if city := strings.TrimSpace(q.City); city != "" {
		// Backticks quote the value, so a city with a space or a comma cannot
		// break out of the expression.
		filters = append(filters, fmt.Sprintf("ort:=`%s`", escapeFilter(city)))
	}
	if providerType := strings.TrimSpace(q.Type); providerType != "" {
		filters = append(filters, fmt.Sprintf("type:=`%s`", escapeFilter(providerType)))
	}
	return strings.Join(filters, " && ")
}

// escapeFilter removes the one character that would end a quoted value early.
// The type is validated against the enum before reaching here; the city is free
// text and is the reason this exists.
func escapeFilter(value string) string {
	return strings.ReplaceAll(value, "`", "")
}

// Ping reports whether the engine answers, for a readiness probe.
func (c *TypesenseClient) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/health", nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-TYPESENSE-API-KEY", c.apiKey)

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: health returned %s", ErrUnavailable, resp.Status)
	}
	return nil
}

// Ensure TypesenseClient satisfies Client at compile time.
var _ Client = (*TypesenseClient)(nil)
