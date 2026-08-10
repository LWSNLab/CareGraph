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
				"path", c.Request.URL.Path,
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
