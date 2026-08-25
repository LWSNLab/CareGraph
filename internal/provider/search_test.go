package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/search"
	"github.com/gin-gonic/gin"
)

// fakeSearch stands in for the engine.
type fakeSearch struct {
	got    search.Query
	result *search.Result
	err    error
}

func (f *fakeSearch) Search(_ context.Context, q search.Query) (*search.Result, error) {
	f.got = q
	if f.err != nil {
		return nil, f.err
	}
	return f.result, nil
}

func (f *fakeSearch) Ping(context.Context) error { return f.err }

func searchServer(repo Repository, engine search.Client, w io.Writer) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(httpx.RequestID())
	h := NewHandler(repo).WithLogger(slog.New(slog.NewTextHandler(w, nil)))
	if engine != nil {
		h = h.WithSearch(engine)
	}
	r.GET("/v1/infrastructure/search", h.Search)
	return r
}

func search1(t *testing.T, r *gin.Engine, query string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/v1/infrastructure/search?"+query, nil))
	return w
}

func TestSearchRejectsBadInputBeforeReachingTheEngine(t *testing.T) {
	bad := map[string]string{
		"missing q":     "",
		"q too short":   "q=a",
		"q empty":       "q=",
		"limit zero":    "q=test&limit=0",
		"limit too big": "q=test&limit=101",
		"limit text":    "q=test&limit=many",
		"unknown type":  "q=test&type=apotheke",
		"q too long":    "q=" + url.QueryEscape(strings.Repeat("x", 200)),
	}
	for name, query := range bad {
		t.Run(name, func(t *testing.T) {
			engine := &fakeSearch{result: &search.Result{}}
			w := search1(t, searchServer(&fakeRepo{}, engine, io.Discard), query)

			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400 (body %s)", w.Code, w.Body)
			}
			if engine.got.Text != "" {
				t.Error("the engine was queried despite invalid input")
			}
		})
	}
}

func TestSearchHydratesFromPostgresInEngineOrder(t *testing.T) {
	// The whole point of the two-stage design: the engine ranks, the database
	// fills in, and the ranking survives.
	engine := &fakeSearch{result: &search.Result{
		IDs:   []string{"stoid:3", "osm:node/1", "osm:node/2"},
		Found: 99,
	}}
	repo := &fakeRepo{byIDResult: []Provider{
		{ID: "c", Name: "Third"}, {ID: "a", Name: "First"}, {ID: "b", Name: "Second"},
	}}

	w := search1(t, searchServer(repo, engine, io.Discard), "q=charite&limit=3")
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body %s)", w.Code, w.Body)
	}

	// The identifiers reach the repository unchanged and in order.
	if strings.Join(repo.gotIDs, ",") != "stoid:3,osm:node/1,osm:node/2" {
		t.Errorf("repo received %v", repo.gotIDs)
	}

	var body struct {
		Total int        `json:"total"`
		Data  []Provider `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	// `total` is the engine's match count, not the page length — a client needs
	// it to know there is more behind the limit.
	if body.Total != 99 {
		t.Errorf("total = %d, want the engine's 99", body.Total)
	}
	if len(body.Data) != 3 || body.Data[0].Name != "Third" {
		t.Errorf("data lost the repository's order: %+v", body.Data)
	}
}

func TestSearchPassesFiltersThrough(t *testing.T) {
	engine := &fakeSearch{result: &search.Result{}}
	search1(t, searchServer(&fakeRepo{}, engine, io.Discard),
		"q=charite&city=Berlin&type=krankenhaus&limit=7")

	if engine.got.Text != "charite" || engine.got.City != "Berlin" {
		t.Errorf("engine received %+v", engine.got)
	}
	if engine.got.Type != "krankenhaus" || engine.got.Limit != 7 {
		t.Errorf("engine received %+v", engine.got)
	}
}

func TestSearchWithoutAnEngineIsNotImplemented(t *testing.T) {
	// A deployment that does not run Typesense stays usable; the endpoint says
	// so rather than erroring as though something broke.
	w := search1(t, searchServer(&fakeRepo{}, nil, io.Discard), "q=charite")

	if w.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501", w.Code)
	}
	var body httpx.ErrorBody
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body.Code != httpx.CodeNotImplemented {
		t.Errorf("code = %q", body.Code)
	}
}

func TestAnUnavailableEngineIs503NotAnEmptyResult(t *testing.T) {
	// 200 with no results would tell the user their search found nothing, which
	// is a different and wrong statement. 503 says "retry", which is true.
	var logged bytes.Buffer
	engine := &fakeSearch{err: search.ErrUnavailable}

	w := search1(t, searchServer(&fakeRepo{}, engine, &logged), "q=charite")

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 (body %s)", w.Code, w.Body)
	}
	var body httpx.ErrorBody
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body.Code != httpx.CodeUnavailable {
		t.Errorf("code = %q, want %q", body.Code, httpx.CodeUnavailable)
	}
	if !strings.Contains(logged.String(), "unavailable") {
		t.Errorf("the cause was not logged: %s", logged.String())
	}
}

func TestASearchTimeoutIs504(t *testing.T) {
	engine := &fakeSearch{err: context.DeadlineExceeded}
	w := search1(t, searchServer(&fakeRepo{}, engine, io.Discard), "q=charite")

	if w.Code != http.StatusGatewayTimeout {
		t.Errorf("status = %d, want 504", w.Code)
	}
}

func TestAnUnexpectedEngineErrorIs500AndDoesNotLeak(t *testing.T) {
	var logged bytes.Buffer
	engine := &fakeSearch{err: errors.New("apikey=hunter2 rejected")}

	w := search1(t, searchServer(&fakeRepo{}, engine, &logged), "q=charite")

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", w.Code)
	}
	if strings.Contains(w.Body.String(), "hunter2") {
		t.Errorf("the engine error leaked to the client: %s", w.Body)
	}
	if !strings.Contains(logged.String(), "hunter2") {
		t.Errorf("the cause was not logged: %s", logged.String())
	}
}

func TestNoHitsSerialisesAsAnEmptyArray(t *testing.T) {
	engine := &fakeSearch{result: &search.Result{IDs: nil, Found: 0}}
	repo := &fakeRepo{byIDResult: nil}

	w := search1(t, searchServer(repo, engine, io.Discard), "q=charite")

	var body struct {
		Data *[]Provider `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Data == nil {
		t.Errorf("data = null, want [] (body %s)", w.Body)
	}
}
