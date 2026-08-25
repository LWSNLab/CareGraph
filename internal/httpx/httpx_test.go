package httpx

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
	"time"

	"github.com/gin-gonic/gin"
)

func newEngine(log *slog.Logger) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.HandleMethodNotAllowed = true
	r.Use(RequestID(), Recovery(log))
	r.NoRoute(NoRoute())
	r.NoMethod(NoMethod())
	return r
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
}

func do(t *testing.T, r *gin.Engine, method, target string, header http.Header) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, target, nil)
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func decode(t *testing.T, w *httptest.ResponseRecorder) ErrorBody {
	t.Helper()
	var body ErrorBody
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not the error shape: %v (raw: %s)", err, w.Body)
	}
	return body
}

// ------------------------------------------------------------ routing errors

func TestUnknownRouteAnswersInTheErrorShape(t *testing.T) {
	r := newEngine(discardLogger())
	r.GET("/v1/known", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })

	w := do(t, r, http.MethodGet, "/v1/nonsense", nil)

	if w.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", w.Code)
	}
	// Gin's default is `404 page not found` as text/plain, which a client that
	// parses every response as JSON cannot read.
	if ct := w.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	body := decode(t, w)
	if body.Code != CodeNotFound {
		t.Errorf("code = %q, want %q", body.Code, CodeNotFound)
	}
	if body.Error == "" {
		t.Error("error message is empty")
	}
}

func TestWrongMethodIs405WithAllow(t *testing.T) {
	r := newEngine(discardLogger())
	r.GET("/v1/known", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })

	w := do(t, r, http.MethodPost, "/v1/known", nil)

	if w.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405 — without HandleMethodNotAllowed Gin reports 404", w.Code)
	}
	// RFC 9110 §15.5.6 requires Allow on a 405.
	if allow := w.Header().Get("Allow"); !strings.Contains(allow, http.MethodGet) {
		t.Errorf("Allow = %q, want it to list GET", allow)
	}
	if code := decode(t, w).Code; code != CodeMethodNotAllowed {
		t.Errorf("code = %q, want %q", code, CodeMethodNotAllowed)
	}
}

// -------------------------------------------------------------- request ids

func TestGeneratedRequestIDIsEchoedAndUnique(t *testing.T) {
	r := newEngine(discardLogger())
	r.GET("/v1/known", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })

	seen := map[string]bool{}
	for range 5 {
		w := do(t, r, http.MethodGet, "/v1/known", nil)
		id := w.Header().Get(HeaderRequestID)
		if id == "" {
			t.Fatal("no request id on the response")
		}
		if seen[id] {
			t.Fatalf("request id %q was reused", id)
		}
		seen[id] = true
	}
}

func TestClientSuppliedRequestIDIsReused(t *testing.T) {
	r := newEngine(discardLogger())
	r.GET("/v1/known", func(c *gin.Context) {
		// Visible to handlers via both lookups.
		if got := RequestIDFromGin(c); got != "trace-abc_123.4" {
			t.Errorf("RequestIDFromGin = %q", got)
		}
		if got := RequestIDFromContext(c.Request.Context()); got != "trace-abc_123.4" {
			t.Errorf("RequestIDFromContext = %q", got)
		}
		c.JSON(200, gin.H{"ok": true})
	})

	w := do(t, r, http.MethodGet, "/v1/known",
		http.Header{HeaderRequestID: []string{"trace-abc_123.4"}})

	if got := w.Header().Get(HeaderRequestID); got != "trace-abc_123.4" {
		t.Errorf("echoed id = %q, want the client's", got)
	}
}

func TestHostileRequestIDIsReplaced(t *testing.T) {
	// Client ids are untrusted: they reach a response header and every log line
	// for the request. Anything outside the safe alphabet is discarded rather
	// than repaired.
	hostile := []struct {
		name string
		raw  string
	}{
		{"crlf injection", "abc\r\nX-Evil: 1"},
		{"newline", "abc\ndef"},
		{"log forging", "abc level=ERROR msg=fake"},
		{"space", "abc def"},
		{"quote", `abc"def`},
		{"semicolon", "abc;def"},
		{"unicode", "abc·def"},
		{"too long", strings.Repeat("a", maxRequestIDLen+1)},
	}

	for _, tc := range hostile {
		t.Run(tc.name, func(t *testing.T) {
			r := newEngine(discardLogger())
			r.GET("/v1/known", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })

			w := do(t, r, http.MethodGet, "/v1/known",
				http.Header{HeaderRequestID: []string{tc.raw}})

			got := w.Header().Get(HeaderRequestID)
			if got == tc.raw {
				t.Fatalf("hostile id was accepted verbatim: %q", got)
			}
			if got == "" {
				t.Fatal("no replacement id was generated")
			}
			if len(got) > maxRequestIDLen {
				t.Errorf("replacement id is %d bytes, over the cap", len(got))
			}
			for i := 0; i < len(got); i++ {
				if !isRequestIDByte(got[i]) {
					t.Fatalf("replacement id contains an unsafe byte: %q", got)
				}
			}
		})
	}
}

func TestRequestIDAppearsInErrorBodies(t *testing.T) {
	r := newEngine(discardLogger())

	w := do(t, r, http.MethodGet, "/v1/nonsense",
		http.Header{HeaderRequestID: []string{"corr-1"}})

	if id := decode(t, w).RequestID; id != "corr-1" {
		t.Errorf("request_id in body = %q, want %q", id, "corr-1")
	}
}

func TestErrorBodyOmitsRequestIDWithoutMiddleware(t *testing.T) {
	// Handlers used without the middleware must still produce valid bodies.
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/boom", func(c *gin.Context) {
		Fail(c, http.StatusBadRequest, CodeInvalidParameter, "nope")
	})

	w := do(t, r, http.MethodGet, "/boom", nil)
	if strings.Contains(w.Body.String(), "request_id") {
		t.Errorf("body = %s, want request_id omitted", w.Body)
	}
}

// ---------------------------------------------------------------- recovery

func TestPanicIsLoggedAndAnsweredInTheErrorShape(t *testing.T) {
	var logged bytes.Buffer
	r := newEngine(slog.New(slog.NewTextHandler(&logged, &slog.HandlerOptions{Level: slog.LevelDebug})))
	r.GET("/boom", func(c *gin.Context) { panic("kaboom") })

	w := do(t, r, http.MethodGet, "/boom",
		http.Header{HeaderRequestID: []string{"corr-panic"}})

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", w.Code)
	}
	// gin.Recovery() writes an empty body here — the one failure a client
	// could not parse like every other.
	body := decode(t, w)
	if body.Code != CodeInternal {
		t.Errorf("code = %q, want %q", body.Code, CodeInternal)
	}
	if body.RequestID != "corr-panic" {
		t.Errorf("request_id = %q, want the correlation id", body.RequestID)
	}
	// The panic value must not be handed to the client.
	if strings.Contains(w.Body.String(), "kaboom") {
		t.Errorf("panic value leaked: %s", w.Body)
	}

	out := logged.String()
	for _, want := range []string{"kaboom", "corr-panic", "level=ERROR", "stack"} {
		if !strings.Contains(out, want) {
			t.Errorf("log is missing %q; got: %s", want, out)
		}
	}
}

func TestPanicAfterHeadersDoesNotCorruptTheResponse(t *testing.T) {
	var logged bytes.Buffer
	r := newEngine(slog.New(slog.NewTextHandler(&logged, nil)))
	r.GET("/late", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"partial": true})
		panic("too late")
	})

	w := do(t, r, http.MethodGet, "/late", nil)

	// The status line is already spent; appending an error body would produce
	// a response that is neither the success nor the failure.
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want the already-sent 200", w.Code)
	}
	if strings.Contains(w.Body.String(), "internal error") {
		t.Errorf("error body was appended to a sent response: %s", w.Body)
	}
	if !strings.Contains(logged.String(), "too late") {
		t.Errorf("panic was not logged: %s", logged.String())
	}
}

// ----------------------------------------------------------------- timeout

func TestTimeoutCancelsTheRequestContext(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(Timeout(20 * time.Millisecond))

	var ctxErr error
	r.GET("/slow", func(c *gin.Context) {
		select {
		case <-c.Request.Context().Done():
			ctxErr = c.Request.Context().Err()
		case <-time.After(2 * time.Second):
		}
		c.Status(http.StatusOK)
	})

	do(t, r, http.MethodGet, "/slow", nil)

	if !errors.Is(ctxErr, context.DeadlineExceeded) {
		t.Errorf("context error = %v, want DeadlineExceeded", ctxErr)
	}
}

func TestLoggerAttachesRequestID(t *testing.T) {
	var logged bytes.Buffer
	base := slog.New(slog.NewTextHandler(&logged, nil))

	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(RequestID())
	r.GET("/log", func(c *gin.Context) {
		Logger(base, c).Info("hello")
		c.Status(http.StatusOK)
	})

	do(t, r, http.MethodGet, "/log", http.Header{HeaderRequestID: []string{"corr-log"}})

	if !strings.Contains(logged.String(), "request_id=corr-log") {
		t.Errorf("log record has no request_id: %s", logged.String())
	}
}
