package httpx

import (
	"bytes"
	"log/slog"
	"net/http"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestRedactQuery(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"empty", "", ""},
		{"listed parameters keep their values",
			"radius_km=5&limit=20&type=pflegedienst",
			"limit=20&radius_km=5&type=pflegedienst"},
		{"a radius query keeps everything but the point",
			"lat=52.52&lng=13.405&radius_km=5",
			"lat=REDACTED&lng=REDACTED&radius_km=5"},
		{"a search term is a caller's interest, not a parameter",
			"q=Charite&limit=20", "limit=20&q=REDACTED"},
		// The reason this is an allowlist. A parameter nobody thought about is
		// redacted by default instead of being logged until somebody notices.
		{"an unknown parameter is redacted without anyone adding it",
			"postal_code=48143", "postal_code=REDACTED"},
		{"every repetition, not just the first",
			"lat=52.52&lat=48.13", "lat=REDACTED&lat=REDACTED"},
		// url.ParseQuery decodes the name before this sees it, which is why the
		// implementation parses rather than scanning for a literal "lat=".
		{"a percent-encoded name is still recognised", "%6Cat=52.52", "lat=REDACTED"},
		{"case does not help either", "LAT=52.52", "LAT=REDACTED"},
		{"an unparseable query is dropped whole", "lat=52.52&%zz", "[unparsable]"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := RedactQuery(tc.in); got != tc.want {
				t.Fatalf("RedactQuery(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// MaxHeaderBytes is unset, so Go accepts roughly a megabyte of request line. The
// access log runs on every request, and without a bound one caller could fill
// the log disk from a single endpoint.
func TestRedactQueryIsBounded(t *testing.T) {
	if got := RedactQuery("q=" + strings.Repeat("x", maxQueryBytes)); got != "[oversized]" {
		t.Errorf("an oversized query was not rejected before parsing: %q", got)
	}

	// Names survive redaction, so many short parameters reach the cap without any
	// single value being large.
	var b strings.Builder
	for i := 0; b.Len() < maxLoggedBytes; i++ {
		b.WriteString("aaaaaaaaaaaaaaaaaaaa")
		b.WriteString("=1&")
		b.WriteByte(byte('a' + i%26))
	}
	got := RedactQuery(b.String())
	if len(got) > maxLoggedBytes+len("…[truncated]") {
		t.Errorf("result is %d bytes, over the cap", len(got))
	}
	if !strings.HasSuffix(got, "…[truncated]") {
		t.Errorf("a truncated result does not say so: %q", got)
	}
}

// The test that matters: a real request through the access log, checked against
// the bytes that would reach an aggregator. RedactQuery being correct is worth
// nothing if a caller forgets to use it.
func TestAccessLogNeverRecordsACoordinate(t *testing.T) {
	const (
		lat = "52.5200066"
		lng = "13.4049540"
	)

	var logged bytes.Buffer
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(RequestID(), AccessLog(slog.New(slog.NewJSONHandler(&logged,
		&slog.HandlerOptions{Level: slog.LevelDebug}))))
	r.GET("/v1/infrastructure/near", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"total": 0})
	})

	w := do(t, r, http.MethodGet,
		"/v1/infrastructure/near?lat="+lat+"&lng="+lng+"&radius_km=5", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}

	out := logged.String()
	for _, coordinate := range []string{lat, lng} {
		if strings.Contains(out, coordinate) {
			t.Errorf("the access log records the coordinate %s:\n%s", coordinate, out)
		}
	}

	// Without this the test would still pass if the query were dropped entirely,
	// which would take the operational value with it.
	for _, keep := range []string{"radius_km=5", "REDACTED"} {
		if !strings.Contains(out, keep) {
			t.Errorf("the access log lost %q:\n%s", keep, out)
		}
	}
}
