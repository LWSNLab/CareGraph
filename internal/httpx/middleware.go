package httpx

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"runtime/debug"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
)

// Logger returns the request-scoped logger: base plus this request's
// correlation id. Falls back to base when no RequestID middleware ran.
func Logger(base *slog.Logger, c *gin.Context) *slog.Logger {
	if id := RequestIDFromGin(c); id != "" {
		return base.With("request_id", id)
	}
	return base
}

// AccessLog writes one structured record per request.
//
// Replaces gin.Logger(), which writes a plain-text line to its own writer. The
// rest of the service logs JSON through slog, and two formats in one stream
// defeat the point of structured logging: an aggregator either drops the
// odd-shaped lines or stores them as unsearchable text. The access log is also
// the highest-volume producer, so it is the worst one to leave unparseable.
//
// The level carries the meaning, so a filter alone separates the interesting
// records: client mistakes are warnings, server failures are errors, and a
// caller that hung up is neither.
func AccessLog(log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		started := time.Now()

		// Captured before c.Next(): a handler may rewrite the query, and the
		// record should describe what was asked for. Redacted here rather than at
		// the point of use, so no later edit can log the raw form by accident.
		path, query := SafePath(c.Request.URL.Path), RedactQuery(c.Request.URL.RawQuery)

		c.Next()

		status := c.Writer.Status()
		attrs := []any{
			"method", c.Request.Method,
			"path", path,
			"status", status,
			"duration_ms", float64(time.Since(started).Microseconds()) / 1000,
			"bytes", c.Writer.Size(),
			"client_ip", c.ClientIP(),
		}
		if query != "" {
			attrs = append(attrs, "query", query)
		}

		ctx := c.Request.Context()
		entry := Logger(log, c)

		switch {
		// The caller went away. Nothing failed here and nobody is waiting for an
		// answer, so this must not be logged as a server error — it would put a
		// flaky client's disconnects into the same bucket as real 5xx.
		case status == StatusClientClosedRequest:
			entry.DebugContext(ctx, "client closed request", attrs...)
		case status >= http.StatusInternalServerError:
			entry.ErrorContext(ctx, "request failed", attrs...)
		case status >= http.StatusBadRequest:
			entry.WarnContext(ctx, "request rejected", attrs...)
		default:
			entry.InfoContext(ctx, "request", attrs...)
		}
	}
}

// Recovery turns a panic into a logged incident and a response in the standard
// error shape. gin.Recovery() writes an empty 500 body, which means a panic is
// the one failure a client cannot parse like any other.
func Recovery(log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		defer func() {
			r := recover()
			if r == nil {
				return
			}

			entry := Logger(log, c).With(
				"method", c.Request.Method,
				"path", SafePath(c.Request.URL.Path),
			)

			// The client vanished mid-write. Nothing can be sent and nothing is
			// wrong with the service, so this is not an error-level event.
			if isBrokenPipe(r) {
				entry.DebugContext(c.Request.Context(),
					"connection broken while writing the response", "cause", r)
				c.Abort()
				return
			}

			entry.ErrorContext(c.Request.Context(), "panic recovered",
				"panic", r, "stack", string(debug.Stack()))

			// Headers already flushed: the status line is spent, so appending a
			// JSON body would corrupt the response. Cut the connection instead.
			if c.Writer.Written() {
				c.Abort()
				return
			}
			Abort(c, http.StatusInternalServerError, CodeInternal, "internal error")
		}()

		c.Next()
	}
}

// Timeout bounds the whole request by cancelling its context.
//
// This is cooperative: it stops anything that honours the context — every
// database call does — but it cannot interrupt a handler that ignores it. A
// hard cut would mean writing a response from another goroutine while the
// handler may still be writing its own, which is worse than the gap it closes.
// Server-level write timeouts are the backstop for that case.
func Timeout(d time.Duration) gin.HandlerFunc {
	return func(c *gin.Context) {
		ctx, cancel := context.WithTimeout(c.Request.Context(), d)
		defer cancel()
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	}
}

func isBrokenPipe(r any) bool {
	err, ok := r.(error)
	if !ok {
		return false
	}
	var netErr *net.OpError
	if !errors.As(err, &netErr) {
		return false
	}
	var sysErr *os.SyscallError
	if !errors.As(netErr.Err, &sysErr) {
		return false
	}
	return errors.Is(sysErr.Err, syscall.EPIPE) || errors.Is(sysErr.Err, syscall.ECONNRESET)
}
