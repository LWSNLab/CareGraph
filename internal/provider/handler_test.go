package provider

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

// fakeRepo records what the handler passed down and returns canned results.
type fakeRepo struct {
	got    NearParams
	called bool
	result []Provider
	err    error
}

func (f *fakeRepo) Near(_ context.Context, p NearParams) ([]Provider, error) {
	f.got, f.called = p, true
	return f.result, f.err
}

func (f *fakeRepo) GetByIK(context.Context, string) (*Provider, error) {
	return nil, ErrNotImplemented
}

func newTestServer(repo Repository) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h := NewHandler(repo)
	r.GET("/v1/infrastructure/near", h.Near)
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
	// The driver error must not reach the client.
	if body := w.Body.String(); body != `{"error":"internal error"}` {
		t.Errorf("body = %s, want a generic error", body)
	}
}

func TestHealth(t *testing.T) {
	w := get(t, newTestServer(&fakeRepo{}), "/healthz")
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", w.Code)
	}
}
