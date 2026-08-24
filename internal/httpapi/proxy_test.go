package httpapi

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

// clientIPSeenBy reports what ClientIP() resolves to — the key for both the tier
// quota and the failed-auth budget, so it decides who shares a bucket.
func clientIPSeenBy(t *testing.T, trusted []string, remoteAddr, forwardedFor string) string {
	t.Helper()
	gin.SetMode(gin.TestMode)

	r, err := NewRouter(Deps{
		TrustedProxies: trusted,
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("build router: %v", err)
	}

	var seen string
	r.GET("/whoami", func(c *gin.Context) { seen = c.ClientIP(); c.Status(http.StatusOK) })

	req := httptest.NewRequest(http.MethodGet, "/whoami", nil)
	req.RemoteAddr = remoteAddr
	if forwardedFor != "" {
		req.Header.Set("X-Forwarded-For", forwardedFor)
	}
	r.ServeHTTP(httptest.NewRecorder(), req)
	return seen
}

// Otherwise a client could pick a fresh address per request and never exhaust the
// failed-authentication budget.
func TestForgedForwardedForIsIgnoredWithoutATrustedProxy(t *testing.T) {
	got := clientIPSeenBy(t, nil, "203.0.113.9:1234", "1.2.3.4")

	if got != "203.0.113.9" {
		t.Errorf("ClientIP = %q, want the real peer 203.0.113.9 — a spoofed header was trusted", got)
	}
}

// Otherwise every client is bucketed under the proxy's address and one abuser
// locks out everyone.
func TestForwardedForIsUsedBehindATrustedProxy(t *testing.T) {
	got := clientIPSeenBy(t, []string{"172.28.0.0/16"}, "172.28.0.5:5678", "198.51.100.7")

	if got != "198.51.100.7" {
		t.Errorf("ClientIP = %q, want the forwarded 198.51.100.7 — clients would share one bucket", got)
	}
}

// Trust is per-address, so it must not leak to other peers.
func TestForwardedForFromAnUntrustedPeerIsStillIgnored(t *testing.T) {
	got := clientIPSeenBy(t, []string{"172.28.0.0/16"}, "203.0.113.9:1234", "1.2.3.4")

	if got != "203.0.113.9" {
		t.Errorf("ClientIP = %q, want the real peer — trust leaked beyond the configured proxy", got)
	}
}

func TestAnInvalidTrustedProxyIsAConfigurationError(t *testing.T) {
	_, err := NewRouter(Deps{
		TrustedProxies: []string{"not-a-cidr"},
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err == nil {
		t.Error("an unparseable trusted proxy was accepted")
	}
}
