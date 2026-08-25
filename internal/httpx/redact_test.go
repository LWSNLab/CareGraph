package httpx

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"unicode/utf8"

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

func TestSafePath(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"an ordinary path is untouched",
			"/v1/infrastructure/near", "/v1/infrastructure/near"},
		// url.Parse decodes %0A into a real newline, so a path — unlike a query —
		// reaches the logger with control characters still in it.
		{"a decoded newline is encoded again", "/v1/a\nb", "/v1/a%0Ab"},
		{"so is an ANSI escape, which a terminal would otherwise obey",
			"/v1/\x1b[2J", "/v1/%1B[2J"},
		{"and DEL", "/v1/\x7f", "/v1/%7F"},
		{"UTF-8 survives, because only bytes below 0x20 are touched",
			"/v1/Münster", "/v1/Münster"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := SafePath(tc.in); got != tc.want {
				t.Fatalf("SafePath(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// Properties that must hold for any input at all, rather than for the cases
// somebody thought of.
//
// The first of them answers a review suggestion to strip control characters from
// the result: url.Values.Encode percent-encodes names and values alike, so a
// newline or an ANSI escape leaves as %0A or %1B and a stripping pass would be
// dead code. Asserted here instead of argued, and it stays asserted if the
// encoding ever changes.
func FuzzRedactQuery(f *testing.F) {
	for _, seed := range []string{
		"", "lat=52.52&lng=13.405&radius_km=5", "q=Charité&city=Münster",
		"%6Cat=1", "na\x1b[2Jme\x00=va\nlue", "lat=52.52&%zz",
		"type=pflegedienst&limit=20",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, raw string) {
		got := RedactQuery(raw)

		for i := 0; i < len(got); i++ {
			if got[i] < 0x20 || got[i] == 0x7f {
				t.Fatalf("control byte %#02x survived: %q", got[i], got)
			}
		}
		if cap := maxLoggedBytes + len("…[truncated]"); len(got) > cap {
			t.Fatalf("result is %d bytes, cap is %d: %q", len(got), cap, got)
		}

		switch {
		case got == "", got == "[unparsable]", got == "[oversized]":
			return
		case strings.HasSuffix(got, "…[truncated]"):
			// A cut can land inside a token, so the shape below is not expected.
			return
		}

		values, err := url.ParseQuery(got)
		if err != nil {
			t.Fatalf("the result does not parse as a query: %v (%q)", err, got)
		}
		for name, vals := range values {
			if _, loggable := loggableParams[strings.ToLower(name)]; loggable {
				continue
			}
			for _, v := range vals {
				if v != redactedValue {
					t.Fatalf("%q kept the value %q, from input %q", name, v, raw)
				}
			}
		}
	})
}

// The same two properties for paths, which carry real control characters where a
// query carries their escaped form. A review suggested deleting them; this
// asserts that they are encoded instead, so two paths that differ still differ
// in the log — a deletion would map them onto one string and make a forged path
// indistinguishable from a real one in a search.
func FuzzSafePath(f *testing.F) {
	for _, seed := range []string{
		"", "/v1/infrastructure/near", "/v1/a\nb", "/v1/\x1b[2J\x7f",
		"/v1/Münster", "/" + strings.Repeat("ä", 400),
		// A control character landing exactly on the cut, which is the only place
		// it can push the result over the cap: budgeted as one byte but written as
		// three. The first implementation got this wrong across the whole string —
		// it trimmed first and escaped afterwards — and fuzzing found it at 528
		// bytes against a cap of 526. Anywhere earlier in the path the mistake is
		// invisible, because the loop measures what it has already written.
		strings.Repeat("a", maxLoggedBytes-2) + "\x00" + "aaaa",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, raw string) {
		got := SafePath(raw)

		for i := 0; i < len(got); i++ {
			if got[i] < 0x20 || got[i] == 0x7f {
				t.Fatalf("control byte %#02x survived: %q", got[i], got)
			}
		}
		if cap := maxLoggedBytes + len(truncationMark); len(got) > cap {
			t.Fatalf("result is %d bytes, cap is %d", len(got), cap)
		}
		// The cut lands on a rune boundary, so valid input stays valid.
		if utf8.ValidString(raw) && !utf8.ValidString(got) {
			t.Fatalf("valid UTF-8 in, invalid out: %q", got)
		}
	})
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
