package auth

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/gin-gonic/gin"
)

// fakeStore answers from a fixed table and counts how often it was asked, so a
// test can assert that an expensive verification was skipped.
type fakeStore struct {
	valid map[string]*Identity
	err   error
	calls int
}

func (f *fakeStore) Verify(_ context.Context, presented string) (*Identity, error) {
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	if _, _, err := SplitKey(presented); err != nil {
		return nil, err
	}
	if identity, ok := f.valid[presented]; ok {
		return identity, nil
	}
	return nil, ErrNoSuchKey
}

func discard() *slog.Logger {
	return slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
}

// server wires only the auth middleware — the limiter is nil, which the
// middleware tolerates, so these tests need no Redis.
func server(store KeyStore, log *slog.Logger) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(httpx.RequestID())
	v1 := r.Group("/v1")
	v1.Use(APIKeyMiddleware(store, nil, log))
	v1.GET("/thing", func(c *gin.Context) {
		identity := IdentityFrom(c)
		c.JSON(http.StatusOK, gin.H{"name": identity.Name, "tier": identity.Tier})
	})
	return r
}

func call(t *testing.T, r *gin.Engine, key string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/thing", nil)
	if key != "" {
		req.Header.Set(HeaderAPIKey, key)
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func TestValidKeyReachesTheHandler(t *testing.T) {
	key, _, _, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	store := &fakeStore{valid: map[string]*Identity{
		key: {KeyID: "abc", Name: "Acme", Tier: TierEnterprise, LimitPerMin: 6000},
	}}

	w := call(t, server(store, discard()), key)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body %s)", w.Code, w.Body)
	}
	if !strings.Contains(w.Body.String(), "Acme") {
		t.Errorf("handler did not see the identity: %s", w.Body)
	}
}

func TestMissingAndInvalidKeysAreIndistinguishable(t *testing.T) {
	valid, _, _, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	unknown, _, _, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	store := &fakeStore{valid: map[string]*Identity{valid: {KeyID: "abc", Name: "Acme"}}}

	// Telling "no such key id" apart from "wrong secret" would tell an attacker
	// which ids exist, turning one unknown into two smaller ones.
	var bodies []string
	for _, key := range []string{"", "dev", "cg_short", unknown} {
		w := call(t, server(store, discard()), key)
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("key %q: status = %d, want 401", key, w.Code)
		}
		var body httpx.ErrorBody
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
			t.Fatalf("body is not the error shape: %v", err)
		}
		if body.Code != httpx.CodeUnauthorized {
			t.Errorf("key %q: code = %q", key, body.Code)
		}
		bodies = append(bodies, body.Error)
	}
	for _, message := range bodies[1:] {
		if message != bodies[0] {
			t.Errorf("401 messages differ between rejection reasons: %q vs %q", bodies[0], message)
		}
	}
}

func TestMalformedKeyNeverReachesTheStore(t *testing.T) {
	// SplitKey screens the value first, so a scan with random values costs a
	// string check rather than a database round trip and an Argon2id run.
	store := &fakeStore{valid: map[string]*Identity{}}
	r := server(store, discard())

	for _, key := range []string{"dev", "Bearer abc", "cg_nope", strings.Repeat("x", 200)} {
		if w := call(t, r, key); w.Code != http.StatusUnauthorized {
			t.Fatalf("key %q: status = %d, want 401", key, w.Code)
		}
	}
	if store.calls != 4 {
		t.Fatalf("store called %d times; the middleware should delegate screening", store.calls)
	}
}

func TestStoreFailureIsFiveHundredNotUnauthorized(t *testing.T) {
	// A database outage is ours. Answering 401 would send a client chasing its
	// own credentials for a problem on our side.
	key, _, _, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	var logged bytes.Buffer
	store := &fakeStore{err: errors.New("connection refused")}

	w := call(t, server(store, slog.New(slog.NewTextHandler(&logged, nil))), key)

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (body %s)", w.Code, w.Body)
	}
	if strings.Contains(w.Body.String(), "connection refused") {
		t.Errorf("store error leaked to the client: %s", w.Body)
	}
	if !strings.Contains(logged.String(), "connection refused") {
		t.Errorf("cause was not logged: %s", logged.String())
	}
}

func TestPresentedKeyIsNeverLogged(t *testing.T) {
	key, _, _, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	_, secret, _ := SplitKey(key)

	var logged bytes.Buffer
	log := slog.New(slog.NewTextHandler(&logged, &slog.HandlerOptions{Level: slog.LevelDebug}))
	store := &fakeStore{valid: map[string]*Identity{}}

	call(t, server(store, log), key)

	if strings.Contains(logged.String(), secret) {
		t.Errorf("the secret half of a key was written to the log:\n%s", logged.String())
	}
}

func TestRateLimitByKeyFailsClosedWithoutAnIdentity(t *testing.T) {
	// Reachable only by wiring the middleware in the wrong order. Serving the
	// request unlimited would be the worse failure.
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(httpx.RequestID())
	r.GET("/x", RateLimitByKey(nil, discard()), func(c *gin.Context) {
		c.Status(http.StatusOK)
	})

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))

	if w.Code != http.StatusInternalServerError {
		t.Errorf("status = %d, want 500", w.Code)
	}
}

func TestSecurityHeaders(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(SecurityHeaders())
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))

	want := map[string]string{
		"X-Content-Type-Options":  "nosniff",
		"Referrer-Policy":         "no-referrer",
		"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
	}
	for header, value := range want {
		if got := w.Header().Get(header); got != value {
			t.Errorf("%s = %q, want %q", header, got, value)
		}
	}
	// HSTS over plaintext would be ignored by clients anyway; asserting it there
	// only invites confusion about whether TLS is in play.
	if got := w.Header().Get("Strict-Transport-Security"); got != "" {
		t.Errorf("HSTS set on a plaintext request: %q", got)
	}
}

func TestHSTSIsSetBehindATLSTerminatingProxy(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(SecurityHeaders())
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("X-Forwarded-Proto", "https")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if !strings.Contains(w.Header().Get("Strict-Transport-Security"), "max-age=") {
		t.Error("HSTS missing for a request that arrived over TLS")
	}
}

func TestLimitForUsesTheTierDefaultAndTheOverride(t *testing.T) {
	if got := limitFor(TierCommunity, nil); got != DefaultLimitPerMin[TierCommunity] {
		t.Errorf("community default = %d", got)
	}
	if got := limitFor(TierEnterprise, nil); got != DefaultLimitPerMin[TierEnterprise] {
		t.Errorf("enterprise default = %d", got)
	}
	custom := 250
	if got := limitFor(TierCommunity, &custom); got != 250 {
		t.Errorf("override = %d, want 250", got)
	}
	// A nonsensical override must not disable the limit.
	zero := 0
	if got := limitFor(TierCommunity, &zero); got != DefaultLimitPerMin[TierCommunity] {
		t.Errorf("zero override = %d, want the tier default", got)
	}
	// An unknown tier falls back to the most restrictive one rather than to zero.
	if got := limitFor(Tier("gold"), nil); got != DefaultLimitPerMin[TierCommunity] {
		t.Errorf("unknown tier = %d, want the community default", got)
	}
}
