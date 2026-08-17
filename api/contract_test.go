package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/LWSNLab/caregraph/api"
	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/LWSNLab/caregraph/internal/health"
	"github.com/LWSNLab/caregraph/internal/httpapi"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/LWSNLab/caregraph/internal/ratelimit"
	"github.com/LWSNLab/caregraph/internal/search"
	"github.com/getkin/kin-openapi/openapi3filter"
	"github.com/getkin/kin-openapi/routers/gorillamux"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

// The key the fake store accepts. Its shape has to survive auth.SplitKey, which
// runs before the store is consulted.
//
// Assembled from its parts rather than written as one `cg_<id>_<secret>`
// literal. A single string in that shape is indistinguishable from a real
// leaked key to a secret scanner, and the CI gitleaks job flagged exactly this
// line. Splitting it also removes a duplicate: the id half is what stubKeys
// reports back as the verified identity, so it now has one definition.
const (
	testKeyID  = "0123456789abcdef"
	testSecret = "not-a-real-secret"
)

var testAPIKey = "cg_" + testKeyID + "_" + testSecret

// Gin's access log is plain text on stdout and would bury the test output. The
// records these tests care about go through slog, into io.Discard.
func TestMain(m *testing.M) {
	gin.DefaultWriter = io.Discard
	os.Exit(m.Run())
}

// --- fakes ----------------------------------------------------------------

type stubRepo struct {
	near     []provider.Provider
	nearErr  error
	byIK     *provider.Provider
	byIKErr  error
	bySource []provider.Provider
}

func (s stubRepo) Near(context.Context, provider.NearParams) ([]provider.Provider, error) {
	return s.near, s.nearErr
}

func (s stubRepo) GetByIK(context.Context, string) (*provider.Provider, error) {
	return s.byIK, s.byIKErr
}

func (s stubRepo) BySourceIDs(context.Context, []string) ([]provider.Provider, error) {
	return s.bySource, nil
}

type stubSearch struct {
	result *search.Result
	err    error
}

func (s stubSearch) Search(context.Context, search.Query) (*search.Result, error) {
	return s.result, s.err
}

func (s stubSearch) Ping(context.Context) error { return nil }

type stubKeys struct{}

func (stubKeys) Verify(_ context.Context, presented string) (*auth.Identity, error) {
	if presented != testAPIKey {
		return nil, auth.ErrNoSuchKey
	}
	return &auth.Identity{
		KeyID: testKeyID, Name: "contract test",
		Tier: auth.TierCommunity, LimitPerMin: 100,
	}, nil
}

// deadRedis makes every script call fail. Both limiters fail open on error (see
// auth.enforce), so the middleware runs its real code path and lets the request
// through — no Redis needed to exercise the routes it guards.
type deadRedis struct{}

var errNoRedis = errors.New("no redis in this test")

func (deadRedis) Eval(ctx context.Context, _ string, _ []string, _ ...any) *redis.Cmd {
	return redis.NewCmdResult(nil, errNoRedis)
}

func (deadRedis) EvalSha(ctx context.Context, _ string, _ []string, _ ...any) *redis.Cmd {
	return redis.NewCmdResult(nil, errNoRedis)
}

func (deadRedis) EvalRO(ctx context.Context, _ string, _ []string, _ ...any) *redis.Cmd {
	return redis.NewCmdResult(nil, errNoRedis)
}

func (deadRedis) EvalShaRO(ctx context.Context, _ string, _ []string, _ ...any) *redis.Cmd {
	return redis.NewCmdResult(nil, errNoRedis)
}

func (deadRedis) ScriptExists(context.Context, ...string) *redis.BoolSliceCmd {
	return redis.NewBoolSliceResult(nil, errNoRedis)
}

func (deadRedis) ScriptLoad(context.Context, string) *redis.StringCmd {
	return redis.NewStringResult("", errNoRedis)
}

// --- fixtures -------------------------------------------------------------

func ptr[T any](v T) *T { return &v }

// fullProvider sets every optional field, so response validation sees each one.
func fullProvider() provider.Provider {
	return provider.Provider{
		ID:                 "c3b9a12e-1234-5678-90ab-cdef12345678",
		IKNummer:           ptr("101576623"),
		Type:               provider.TypeKrankenkasse,
		Name:               "Techniker Krankenkasse",
		ParentOrganization: ptr("TK"),
		Website:            ptr("https://www.tk.de"),
		Address: provider.Address{
			Street: "Bramfelder Straße 140", PostalCode: "22305",
			City: "Hamburg", State: "Hamburg",
		},
		DistanceKm: ptr(1.42),
		Details:    map[string]any{"zusatzbeitrag": 2.45},
	}
}

// minimalProvider sets only what the schema requires. The interesting half of
// the contract: everything else must genuinely be optional, and a `*string` with
// `omitempty` must be absent rather than null.
func minimalProvider() provider.Provider {
	return provider.Provider{
		ID:   "8f14e45f-ceea-467a-9575-4b1c0dbfda2a",
		Type: provider.TypePflegedienstAmbulant,
		Name: "Ambulanter Pflegedienst",
		Address: provider.Address{
			Street: "Bahnhofstraße 12", PostalCode: "86609", City: "Donauwörth",
		},
	}
}

func testRouter(t *testing.T, opts ...func(*httpapi.Deps)) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)

	handler := provider.NewHandler(stubRepo{}).
		WithSearch(stubSearch{result: &search.Result{}}).
		WithLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))

	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	deps := httpapi.Deps{
		Provider: handler,
		Health: health.New(quiet).
			Register("postgres", health.Required, func(context.Context) error { return nil }).
			Register("redis", health.Optional, func(context.Context) error { return nil }).
			Register("search", health.Optional, func(context.Context) error { return nil }),
		Keys:    stubKeys{},
		Limiter: ratelimit.New(deadRedis{}),
		Log:     quiet,
	}
	for _, opt := range opts {
		opt(&deps)
	}

	r, err := httpapi.NewRouter(deps)
	if err != nil {
		t.Fatalf("build router: %v", err)
	}
	return r
}

var errProbe = errors.New("dependency unreachable")

// withProbes rebuilds the checker with the same three dependencies, failing the
// named ones. Keeping the set identical is the point: the difference between
// cases is severity, not which probes exist.
func withProbes(failing map[string]error) func(*httpapi.Deps) {
	return func(d *httpapi.Deps) {
		quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
		probe := func(name string) health.Probe {
			return func(context.Context) error { return failing[name] }
		}
		d.Health = health.New(quiet).
			Register("postgres", health.Required, probe("postgres")).
			Register("redis", health.Optional, probe("redis")).
			Register("search", health.Optional, probe("search"))
	}
}

func withRepo(repo provider.Repository) func(*httpapi.Deps) {
	return func(d *httpapi.Deps) {
		d.Provider = provider.NewHandler(repo).
			WithSearch(stubSearch{result: &search.Result{}}).
			WithLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))
	}
}

func withRepoAndSearch(repo provider.Repository, engine search.Client) func(*httpapi.Deps) {
	return func(d *httpapi.Deps) {
		h := provider.NewHandler(repo).
			WithLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))
		if engine != nil {
			h = h.WithSearch(engine)
		}
		d.Provider = h
	}
}

// --- response validation --------------------------------------------------

// TestResponsesValidateAgainstSpec drives the real router and checks each answer
// against the document: status code documented, headers and body matching the
// schema.
//
// This is the check that catches a field renamed in Go but not in the spec, a
// required field that stops being emitted, or a status code a handler learned to
// return while the document still claims it cannot happen.
func TestResponsesValidateAgainstSpec(t *testing.T) {
	cases := []struct {
		name    string
		target  string
		noKey   bool
		want    int
		options []func(*httpapi.Deps)
	}{
		{
			name:   "healthz needs no key",
			target: "/healthz", noKey: true, want: http.StatusOK,
		},
		{
			name:   "readyz with every dependency up",
			target: "/readyz", noKey: true, want: http.StatusOK,
		},
		{
			// Redis is optional: quotas stop being enforced, requests still
			// succeed. Pulling the instance out of rotation would remove working
			// capacity to fix nothing.
			name:   "readyz stays ready when an optional dependency is down",
			target: "/readyz", noKey: true, want: http.StatusOK,
			options: []func(*httpapi.Deps){withProbes(map[string]error{"redis": errProbe})},
		},
		{
			// Postgres is required: every endpoint reads from it.
			name:   "readyz reports unavailable when a required dependency is down",
			target: "/readyz", noKey: true, want: http.StatusServiceUnavailable,
			options: []func(*httpapi.Deps){withProbes(map[string]error{"postgres": errProbe})},
		},
		{
			name:   "near returns a fully populated record",
			target: "/v1/infrastructure/near?lat=52.52&lng=13.405&radius_km=15&limit=20",
			want:   http.StatusOK,
			options: []func(*httpapi.Deps){
				withRepo(stubRepo{near: []provider.Provider{fullProvider()}}),
			},
		},
		{
			name:   "near returns a record with only the required fields",
			target: "/v1/infrastructure/near?lat=52.52&lng=13.405",
			want:   http.StatusOK,
			options: []func(*httpapi.Deps){
				withRepo(stubRepo{near: []provider.Provider{minimalProvider()}}),
			},
		},
		{
			name:   "near with no matches",
			target: "/v1/infrastructure/near?lat=0&lng=0",
			want:   http.StatusOK,
		},
		{
			name:   "near rejects a missing coordinate",
			target: "/v1/infrastructure/near?lat=52.52",
			want:   http.StatusBadRequest,
		},
		{
			name:   "near rejects an out-of-range coordinate",
			target: "/v1/infrastructure/near?lat=91&lng=13.405",
			want:   http.StatusBadRequest,
		},
		{
			name:   "lookup returns the institution",
			target: "/v1/infrastructure/101576623",
			want:   http.StatusOK,
			options: []func(*httpapi.Deps){
				withRepo(stubRepo{byIK: ptr(fullProvider())}),
			},
		},
		{
			name:   "lookup of an unknown IK",
			target: "/v1/infrastructure/999999999",
			want:   http.StatusNotFound,
		},
		{
			name:   "lookup rejects a malformed IK",
			target: "/v1/infrastructure/12345",
			want:   http.StatusBadRequest,
		},
		{
			name:   "search hydrates the ranked ids",
			target: "/v1/infrastructure/search?q=Charite&limit=20",
			want:   http.StatusOK,
			options: []func(*httpapi.Deps){
				withRepoAndSearch(
					stubRepo{bySource: []provider.Provider{minimalProvider()}},
					stubSearch{result: &search.Result{IDs: []string{"osm:1"}, Found: 4}},
				),
			},
		},
		{
			name:   "search rejects a one-character query",
			target: "/v1/infrastructure/search?q=a",
			want:   http.StatusBadRequest,
		},
		{
			name:   "search without an engine configured",
			target: "/v1/infrastructure/search?q=Charite",
			want:   http.StatusNotImplemented,
			options: []func(*httpapi.Deps){
				withRepoAndSearch(stubRepo{}, nil),
			},
		},
		{
			name:   "search with the engine down",
			target: "/v1/infrastructure/search?q=Charite",
			want:   http.StatusServiceUnavailable,
			options: []func(*httpapi.Deps){
				withRepoAndSearch(stubRepo{}, stubSearch{err: search.ErrUnavailable}),
			},
		},
		{
			name:   "a missing key is rejected",
			target: "/v1/infrastructure/near?lat=52.52&lng=13.405",
			noKey:  true, want: http.StatusUnauthorized,
		},
	}

	doc := loadSpec(t)
	router, err := gorillamux.NewRouter(doc)
	if err != nil {
		t.Fatalf("build spec router: %v", err)
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "http://localhost:8080"+tc.target, nil)
			if !tc.noKey {
				req.Header.Set("X-API-Key", testAPIKey)
			}

			rec := httptest.NewRecorder()
			testRouter(t, tc.options...).ServeHTTP(rec, req)

			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d; body: %s", rec.Code, tc.want, rec.Body)
			}

			route, pathParams, err := router.FindRoute(req)
			if err != nil {
				t.Fatalf("the spec does not route %s %s: %v", req.Method, tc.target, err)
			}

			input := &openapi3filter.RequestValidationInput{
				Request: req, PathParams: pathParams, Route: route,
				Options: &openapi3filter.Options{
					AuthenticationFunc: openapi3filter.NoopAuthenticationFunc,
				},
			}
			// Parity in the other direction. When the handler answers 400 the
			// spec's own constraints must reject the request too, and otherwise
			// accept it. Where the two disagree one of them is wrong: either the
			// document permits something the API refuses, or it forbids something
			// the API accepts — and a client that validates before sending is
			// misled either way.
			specErr := openapi3filter.ValidateRequest(context.Background(), input)
			if tc.want == http.StatusBadRequest {
				if specErr == nil {
					t.Errorf("handler rejected this request, but the spec permits it")
				}
			} else if specErr != nil {
				t.Fatalf("the request this test sends is not legal under the spec: %v", specErr)
			}

			err = openapi3filter.ValidateResponse(context.Background(),
				&openapi3filter.ResponseValidationInput{
					RequestValidationInput: input,
					Status:                 rec.Code,
					Header:                 rec.Header(),
					Body:                   io.NopCloser(bytes.NewReader(rec.Body.Bytes())),
					Options: &openapi3filter.Options{
						IncludeResponseStatus: true,
						AuthenticationFunc:    openapi3filter.NoopAuthenticationFunc,
					},
				})
			if err != nil {
				t.Errorf("response does not match the spec: %v\nbody: %s", err, rec.Body)
			}
		})
	}
}

// TestFrameworkFailuresMatchTheErrorSchema covers the two answers no handler
// writes — an unknown path and a wrong method. They cannot be validated through
// the spec's router (it has no route for them by definition), so the body is
// checked against the Error schema directly. Without this the promise that
// *every* failure has one shape would be untested exactly where it is easiest
// to break.
func TestFrameworkFailuresMatchTheErrorSchema(t *testing.T) {
	schema := loadSpec(t).Components.Schemas["Error"]
	if schema == nil {
		t.Fatal("components.schemas.Error is missing")
	}

	cases := []struct {
		name, method, target string
		want                 int
	}{
		{"unknown path", http.MethodGet, "/v1/does-not-exist/nested", http.StatusNotFound},
		{"unknown top-level path", http.MethodGet, "/nope", http.StatusNotFound},
		{"wrong method on a known path", http.MethodPost, "/healthz", http.StatusMethodNotAllowed},
		{"wrong method on the spec route", http.MethodDelete, "/openapi.yaml", http.StatusMethodNotAllowed},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, tc.target, nil)
			req.Header.Set("X-API-Key", testAPIKey)
			rec := httptest.NewRecorder()
			testRouter(t).ServeHTTP(rec, req)

			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d; body: %s", rec.Code, tc.want, rec.Body)
			}
			if ct := rec.Header().Get("Content-Type"); !bytes.Contains([]byte(ct), []byte("application/json")) {
				t.Errorf("Content-Type = %q, want JSON — a client that parses every response would break here", ct)
			}

			var body any
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("body is not JSON: %v (%s)", err, rec.Body)
			}
			if err := schema.Value.VisitJSON(body); err != nil {
				t.Errorf("body does not match the Error schema: %v\nbody: %s", err, rec.Body)
			}
		})
	}
}

// TestSpecIsServed proves a deployment can hand out the contract it implements,
// unauthenticated and byte-identical to the embedded document.
func TestSpecIsServed(t *testing.T) {
	rec := httptest.NewRecorder()
	testRouter(t).ServeHTTP(rec,
		httptest.NewRequest(http.MethodGet, "/openapi.yaml", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 — the contract must be readable without a key", rec.Code)
	}
	if !bytes.Equal(rec.Body.Bytes(), api.SpecYAML) {
		t.Error("served document differs from the embedded one")
	}
}
