package httpx

import (
	"context"
	"crypto/rand"
	"encoding/hex"

	"github.com/gin-gonic/gin"
)

// HeaderRequestID is the correlation header, read from the request when present
// and always set on the response.
const HeaderRequestID = "X-Request-Id"

// maxRequestIDLen caps a client-supplied id. It lands in a response header and
// in every log record for the request, so an unbounded value is a way to bloat
// logs and to smuggle content into them.
const maxRequestIDLen = 64

type ctxKey struct{}

// ginKey is where the id lives on the gin.Context. Distinct from ctxKey so the
// two lookups cannot be confused.
const ginKey = "caregraph.request_id"

// RequestID assigns every request a correlation id: the client's own if it sent
// a usable one, a fresh one otherwise. The id goes onto the gin context, into
// the request context for layers below, and onto the response header.
func RequestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := sanitiseRequestID(c.GetHeader(HeaderRequestID))
		if id == "" {
			id = newRequestID()
		}

		c.Set(ginKey, id)
		c.Request = c.Request.WithContext(
			context.WithValue(c.Request.Context(), ctxKey{}, id))
		c.Header(HeaderRequestID, id)

		c.Next()
	}
}

// RequestIDFromGin returns the correlation id, or "" if no middleware ran.
func RequestIDFromGin(c *gin.Context) string {
	if c == nil {
		return ""
	}
	if v, ok := c.Get(ginKey); ok {
		if id, ok := v.(string); ok {
			return id
		}
	}
	return ""
}

// RequestIDFromContext returns the correlation id carried on a plain context.
func RequestIDFromContext(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	if id, ok := ctx.Value(ctxKey{}).(string); ok {
		return id
	}
	return ""
}

// sanitiseRequestID accepts a client id only if it is entirely made of
// characters that are safe in a header and in a log line, and short enough to
// be harmless. Anything else yields "" so a generated id is used instead —
// rejecting is safer than trying to repair attacker-controlled input.
func sanitiseRequestID(raw string) string {
	if raw == "" || len(raw) > maxRequestIDLen {
		return ""
	}
	for i := 0; i < len(raw); i++ {
		if !isRequestIDByte(raw[i]) {
			return ""
		}
	}
	return raw
}

func isRequestIDByte(b byte) bool {
	switch {
	case b >= 'a' && b <= 'z',
		b >= 'A' && b <= 'Z',
		b >= '0' && b <= '9',
		b == '-', b == '_', b == '.':
		return true
	}
	return false
}

// newRequestID returns 16 random bytes as hex. crypto/rand so ids cannot be
// guessed or replayed to collide with another request's log trail.
func newRequestID() string {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		// crypto/rand.Read never returns an error on the platforms we target;
		// if that ever changes, an empty id is better than a panic in the
		// first middleware of every request.
		return ""
	}
	return hex.EncodeToString(buf[:])
}
