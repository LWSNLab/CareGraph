package search

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBuildFiltersCombinesCityAndType(t *testing.T) {
	got := buildFilters(Query{City: "Berlin", Type: "krankenhaus"})
	if !strings.Contains(got, "ort:=`Berlin`") || !strings.Contains(got, "type:=`krankenhaus`") {
		t.Errorf("filters = %q", got)
	}
	if !strings.Contains(got, "&&") {
		t.Errorf("filters are not combined: %q", got)
	}
}

func TestNoFiltersWhenNothingIsNarrowed(t *testing.T) {
	if got := buildFilters(Query{Text: "Charite"}); got != "" {
		t.Errorf("filters = %q, want empty", got)
	}
}

func TestABacktickCannotEndTheQuotedValueEarly(t *testing.T) {
	// The city is free text and is interpolated into the engine's filter syntax.
	// A backtick would close the quote and let the rest be read as expression.
	got := buildFilters(Query{City: "Berlin` || type:=krankenhaus"})
	if strings.Count(got, "`") != 2 {
		t.Errorf("value did not stay inside one quoted pair: %q", got)
	}
}

// fakeEngine serves a canned Typesense response.
func fakeEngine(t *testing.T, status int, body any) *TypesenseClient {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-TYPESENSE-API-KEY") != "k" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.WriteHeader(status)
		if body != nil {
			_ = json.NewEncoder(w).Encode(body)
		}
	}))
	t.Cleanup(server.Close)
	return NewTypesenseClient(server.URL, "k")
}

func TestSearchReturnsIdsInEngineOrder(t *testing.T) {
	client := fakeEngine(t, http.StatusOK, map[string]any{
		"found": 42,
		"hits": []map[string]any{
			{"document": map[string]string{"id": "osm:node/3"}},
			{"document": map[string]string{"id": "osm:node/1"}},
			{"document": map[string]string{"id": "stoid:771003"}},
		},
	})

	result, err := client.Search(context.Background(), Query{Text: "x", Limit: 3})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"osm:node/3", "osm:node/1", "stoid:771003"}
	for i, id := range want {
		if result.IDs[i] != id {
			t.Fatalf("ids = %v, want %v", result.IDs, want)
		}
	}
	// `found` is the engine's total, not the page size: a client needs it to
	// know there is more behind the limit.
	if result.Found != 42 {
		t.Errorf("found = %d, want 42", result.Found)
	}
}

func TestAnEngineErrorIsUnavailableNotAnEmptyResult(t *testing.T) {
	// Answering "no results" for an engine that is down would tell a user their
	// search found nothing, which is a different and wrong statement.
	for _, status := range []int{http.StatusNotFound, http.StatusInternalServerError,
		http.StatusServiceUnavailable} {
		client := fakeEngine(t, status, nil)
		_, err := client.Search(context.Background(), Query{Text: "x", Limit: 1})
		if !errors.Is(err, ErrUnavailable) {
			t.Errorf("status %d: err = %v, want ErrUnavailable", status, err)
		}
	}
}

func TestAnUnreachableEngineIsUnavailable(t *testing.T) {
	client := NewTypesenseClient("http://127.0.0.1:1", "k")
	_, err := client.Search(context.Background(), Query{Text: "x", Limit: 1})
	if !errors.Is(err, ErrUnavailable) {
		t.Errorf("err = %v, want ErrUnavailable", err)
	}
}

func TestMalformedJsonIsUnavailable(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer server.Close()

	client := NewTypesenseClient(server.URL, "k")
	_, err := client.Search(context.Background(), Query{Text: "x", Limit: 1})
	if !errors.Is(err, ErrUnavailable) {
		t.Errorf("err = %v, want ErrUnavailable", err)
	}
}

func TestOnlyTheIdIsRequested(t *testing.T) {
	var query string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query = r.URL.RawQuery
		_ = json.NewEncoder(w).Encode(map[string]any{"found": 0, "hits": []any{}})
	}))
	defer server.Close()

	client := NewTypesenseClient(server.URL, "k")
	if _, err := client.Search(context.Background(), Query{Text: "x", Limit: 7}); err != nil {
		t.Fatal(err)
	}
	// The rest of the document comes from Postgres, so asking for it would move
	// bytes nobody reads and invite the two copies to disagree.
	if !strings.Contains(query, "include_fields=id") {
		t.Errorf("query = %q, want include_fields=id", query)
	}
	if !strings.Contains(query, "per_page=7") {
		t.Errorf("query = %q, want per_page=7", query)
	}
	// The name must outweigh the street, or a query matching a street name
	// outranks the facility a user was looking for.
	if !strings.Contains(query, "query_by_weights=8%2C4%2C2%2C1") {
		t.Errorf("query = %q, want the field weights", query)
	}
}
