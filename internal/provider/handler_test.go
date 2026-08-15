package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/gin-gonic/gin"
)

// fakeRepo records what the handler passed down and returns canned results.
type fakeRepo struct {
	got    NearParams
	called bool
	result []Provider
	err    error

	gotIK    string
	ikCalled bool
	ikResult *Provider
	ikErr    error

	gotIDs     []string
	byIDResult []Provider
	byIDErr    error
}

func (f *fakeRepo) Near(_ context.Context, p NearParams) ([]Provider, error) {
	f.got, f.called = p, true
	return f.result, f.err
}

func (f *fakeRepo) GetByIK(_ context.Context, ik string) (*Provider, error) {
	f.gotIK, f.ikCalled = ik, true
	return f.ikResult, f.ikErr
}

func (f *fakeRepo) BySourceIDs(_ context.Context, ids []string) ([]Provider, error) {
	f.gotIDs = ids
	return f.byIDResult, f.byIDErr
}

func newTestServer(repo Repository) *gin.Engine {
	return newTestServerWithLog(repo, io.Discard)
}

// newTestServerWithLog sends handler logs to w so a test can assert on them.
func newTestServerWithLog(repo Repository, w io.Writer) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h := NewHandler(repo).WithLogger(slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	})))
	r.GET("/v1/infrastructure/near", h.Near)
	r.GET("/v1/infrastructure/:ik_nummer", h.GetByIK)
	r.GET("/healthz", h.Health)
	return r
}

func get(t *testing.T, r *gin.Engine, target string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, target, nil))
	return w
}

func TestNearRejectsBadInputWithoutTouchingTheRepository(t *testing.T) {
	bad := []string{
		"/v1/infrastructure/near",
		"/v1/infrastructure/near?lat=52.52",
		"/v1/infrastructure/near?lat=abc&lng=13.405",
		"/v1/infrastructure/near?lat=95&lng=13.405",
		"/v1/infrastructure/near?lat=NaN&lng=13.405",
		"/v1/infrastructure/near?lat=52.52&lng=13.405&radius_km=500",
		"/v1/infrastructure/near?lat=52.52&lng=13.405&limit=0",
		"/v1/infrastructure/near?lat=52.52&lng=13.405&type=apotheke",
	}

	for _, target := range bad {
		t.Run(target, func(t *testing.T) {
			repo := &fakeRepo{}
			w := get(t, newTestServer(repo), target)

			if w.Code != http.StatusBadRequest {
				t.Errorf("status = %d, want 400 (body %s)", w.Code, w.Body)
			}
			if repo.called {
				t.Error("repository was queried despite invalid input")
			}

			var body map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
				t.Fatalf("body is not JSON: %v", err)
			}
			if msg, ok := body["error"].(string); !ok || msg == "" {
				t.Errorf("body = %s, want a non-empty 'error' field", w.Body)
			}
		})
	}
}

func TestNearPassesParsedParamsToRepository(t *testing.T) {
	repo := &fakeRepo{}
	w := get(t, newTestServer(repo),
		"/v1/infrastructure/near?lat=52.52&lng=13.405&radius_km=25&limit=5&type=pflegestuetzpunkt")

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body %s)", w.Code, w.Body)
	}
	if !repo.called {
		t.Fatal("repository was not queried")
	}
	// Guards against a lat/lng transposition between handler and repository.
	if repo.got.Lat != 52.52 || repo.got.Lng != 13.405 {
		t.Errorf("repo received (%v, %v), want (52.52, 13.405)", repo.got.Lat, repo.got.Lng)
	}
	if repo.got.RadiusKm != 25 || repo.got.Limit != 5 {
		t.Errorf("repo received radius=%v limit=%d", repo.got.RadiusKm, repo.got.Limit)
	}
	if repo.got.Type == nil || *repo.got.Type != TypePflegestuetzpunkt {
		t.Errorf("repo received type %v", repo.got.Type)
	}
}

func TestNearSerialisesEmptyResultAsArray(t *testing.T) {
	// A nil slice would marshal to null and break clients that iterate.
	repo := &fakeRepo{result: nil}
	w := get(t, newTestServer(repo), "/v1/infrastructure/near?lat=52.52&lng=13.405")

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	var body struct {
		Total int         `json:"total"`
		Data  *[]Provider `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if body.Data == nil {
		t.Errorf("data = null, want [] (body %s)", w.Body)
	}
	if body.Total != 0 {
		t.Errorf("total = %d, want 0", body.Total)
	}
}

func TestNearReturnsResults(t *testing.T) {
	distance := 1.42
	website := "https://pflegedienst-muster.de"
	repo := &fakeRepo{result: []Provider{{
		ID:         "c3b9a12e-1234-5678-90ab-cdef12345678",
		Type:       TypePflegedienstAmbulant,
		Name:       "Ambulanter Pflegedienst Muster",
		Website:    &website,
		Address:    Address{Street: "Bahnhofstraße 12", PostalCode: "86609", City: "Donauwörth"},
		DistanceKm: &distance,
	}}}

	w := get(t, newTestServer(repo), "/v1/infrastructure/near?lat=52.52&lng=13.405")
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}

	var body struct {
		Total int        `json:"total"`
		Data  []Provider `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if body.Total != 1 || len(body.Data) != 1 {
		t.Fatalf("total=%d len(data)=%d, want 1/1", body.Total, len(body.Data))
	}
	if body.Data[0].DistanceKm == nil || *body.Data[0].DistanceKm != distance {
		t.Errorf("distance_km = %v, want %v", body.Data[0].DistanceKm, distance)
	}
	if body.Data[0].IKNummer != nil {
		t.Errorf("ik_nummer = %v, want omitted", *body.Data[0].IKNummer)
	}
}

func TestNearMapsRepositoryFailureTo500(t *testing.T) {
	repo := &fakeRepo{err: errors.New("connection refused")}
	w := get(t, newTestServer(repo), "/v1/infrastructure/near?lat=52.52&lng=13.405")

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", w.Code)
	}

	var body httpx.ErrorBody
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	// The driver error must not reach the client.
	if body.Error != "internal error" {
		t.Errorf("error = %q, want the generic message", body.Error)
	}
	if body.Code != httpx.CodeInternal {
		t.Errorf("code = %q, want %q", body.Code, httpx.CodeInternal)
	}
}

func TestHealth(t *testing.T) {
	w := get(t, newTestServer(&fakeRepo{}), "/healthz")
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", w.Code)
	}
}

// ------------------------------------------------------------------ GetByIK

func TestGetByIKRejectsMalformedNumbers(t *testing.T) {
	bad := []string{
		"12345678",   // eight digits
		"1234567890", // ten digits
		"12345678a",  // not all digits
		"12345 678",  // whitespace
		"abcdefghi",  // letters
		"-123456789", // sign
	}

	for _, ik := range bad {
		t.Run(ik, func(t *testing.T) {
			repo := &fakeRepo{}
			w := get(t, newTestServer(repo), "/v1/infrastructure/"+url.PathEscape(ik))

			if w.Code != http.StatusBadRequest {
				t.Errorf("status = %d, want 400 (body %s)", w.Code, w.Body)
			}
			// A malformed IK is a client mistake; it must not reach the database.
			if repo.ikCalled {
				t.Error("repository was queried with a malformed IK")
			}
		})
	}
}

func TestGetByIKUnknownNumberIs404(t *testing.T) {
	// Well-formed but absent — distinct from malformed, which is a 400.
	repo := &fakeRepo{ikResult: nil}
	w := get(t, newTestServer(repo), "/v1/infrastructure/999999999")

	if w.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 (body %s)", w.Code, w.Body)
	}
	if !repo.ikCalled || repo.gotIK != "999999999" {
		t.Errorf("repository received %q", repo.gotIK)
	}
}

func TestGetByIKReturnsTheEntity(t *testing.T) {
	ik := "100171007"
	repo := &fakeRepo{ikResult: &Provider{
		ID:       "5f0f5b3c-0000-4000-8000-000000000001",
		IKNummer: &ik,
		Type:     TypeKrankenkasse,
		Name:     "HEK - Hanseatische Krankenkasse",
		Details:  map[string]any{"source": "gkv-spitzenverband"},
	}}

	w := get(t, newTestServer(repo), "/v1/infrastructure/"+ik)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body %s)", w.Code, w.Body)
	}

	var got Provider
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if got.IKNummer == nil || *got.IKNummer != ik {
		t.Errorf("ik_nummer = %v, want %q", got.IKNummer, ik)
	}
	if got.Name != "HEK - Hanseatische Krankenkasse" {
		t.Errorf("name = %q", got.Name)
	}
	// A direct lookup has no reference point, so no distance.
	if got.DistanceKm != nil {
		t.Errorf("distance_km = %v, want omitted", *got.DistanceKm)
	}
}

// ------------------------------------------------------------ error handling

func TestRepositoryErrorIsLoggedButNotLeaked(t *testing.T) {
	var logged bytes.Buffer
	secret := "pgx: dial tcp 10.0.0.5:5432: password authentication failed for user \"caregraph\""
	repo := &fakeRepo{err: errors.New(secret)}

	w := get(t, newTestServerWithLog(repo, &logged),
		"/v1/infrastructure/near?lat=52.52&lng=13.405")

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", w.Code)
	}
	// The driver message can carry host, user and query details.
	if strings.Contains(w.Body.String(), "password") || strings.Contains(w.Body.String(), "10.0.0.5") {
		t.Errorf("driver error leaked to the client: %s", w.Body)
	}
	// But it must not vanish either — a 500 with no logged cause is undiagnosable.
	if !strings.Contains(logged.String(), "password authentication failed") {
		t.Errorf("cause was not logged; log was: %s", logged.String())
	}
	if !strings.Contains(logged.String(), "level=ERROR") {
		t.Errorf("expected an ERROR-level entry, got: %s", logged.String())
	}
}

func TestQueryTimeoutMapsTo504(t *testing.T) {
	var logged bytes.Buffer
	repo := &fakeRepo{err: fmt.Errorf("near query: %w", context.DeadlineExceeded)}

	w := get(t, newTestServerWithLog(repo, &logged),
		"/v1/infrastructure/near?lat=52.52&lng=13.405")

	if w.Code != http.StatusGatewayTimeout {
		t.Fatalf("status = %d, want 504 (body %s)", w.Code, w.Body)
	}
	if !strings.Contains(logged.String(), "timed out") {
		t.Errorf("timeout was not logged: %s", logged.String())
	}
}

func TestClientDisconnectIsNotAServerError(t *testing.T) {
	var logged bytes.Buffer
	repo := &fakeRepo{err: fmt.Errorf("near query: %w", context.Canceled)}
	r := newTestServerWithLog(repo, &logged)

	// A request whose context is already cancelled: the caller gave up.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest(http.MethodGet,
		"/v1/infrastructure/near?lat=52.52&lng=13.405", nil).WithContext(ctx)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	// Nothing failed on our side, so this must not be logged as an error —
	// otherwise a flaky mobile client inflates the server error rate.
	if strings.Contains(logged.String(), "level=ERROR") {
		t.Errorf("client disconnect logged as a server error: %s", logged.String())
	}
	if w.Code != httpx.StatusClientClosedRequest {
		t.Errorf("status = %d, want %d (client closed request)",
			w.Code, httpx.StatusClientClosedRequest)
	}
}

func TestGetByIKMapsRepositoryFailureTo500(t *testing.T) {
	var logged bytes.Buffer
	repo := &fakeRepo{ikErr: errors.New("relation \"care_infrastructure\" does not exist")}

	w := get(t, newTestServerWithLog(repo, &logged), "/v1/infrastructure/100171007")

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (body %s)", w.Code, w.Body)
	}
	if strings.Contains(w.Body.String(), "care_infrastructure") {
		t.Errorf("schema detail leaked to the client: %s", w.Body)
	}
	if !strings.Contains(logged.String(), "care_infrastructure") {
		t.Errorf("cause was not logged: %s", logged.String())
	}
}
