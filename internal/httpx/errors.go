// Package httpx holds the HTTP concerns that are not specific to any domain:
// the single error shape every endpoint answers with, request correlation, and
// the middleware that keeps the framework's own failure paths inside that shape.
package httpx

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

// ErrorCode is a stable identifier a client can branch on. The human-readable
// message may be reworded at any time; these values may not.
type ErrorCode string

// StatusClientClosedRequest is nginx's 499: the caller went away before the
// response was written. Not in net/http because it is not IANA-registered.
const StatusClientClosedRequest = 499

const (
	CodeInvalidParameter ErrorCode = "invalid_parameter"
	CodeUnauthorized     ErrorCode = "unauthorized"
	CodeNotFound         ErrorCode = "not_found"
	CodeMethodNotAllowed ErrorCode = "method_not_allowed"
	CodeRateLimited      ErrorCode = "rate_limited"
	CodeTimeout          ErrorCode = "timeout"
	CodeNotImplemented   ErrorCode = "not_implemented"
	CodeUnavailable      ErrorCode = "unavailable"
	CodeInternal         ErrorCode = "internal"
)

// ErrorBody is the response body for every 4xx and 5xx.
//
// `error` predates the other two fields and keeps its meaning, so adding them
// is backwards compatible: existing clients read the same key they always did.
type ErrorBody struct {
	Error     string    `json:"error"`
	Code      ErrorCode `json:"code"`
	RequestID string    `json:"request_id,omitempty"`
}

// Fail writes the error shape, tagged with this request's correlation id.
func Fail(c *gin.Context, status int, code ErrorCode, message string) {
	c.JSON(status, newErrorBody(c, code, message))
}

// Abort is Fail for middleware: it also stops the handler chain.
func Abort(c *gin.Context, status int, code ErrorCode, message string) {
	c.AbortWithStatusJSON(status, newErrorBody(c, code, message))
}

func newErrorBody(c *gin.Context, code ErrorCode, message string) ErrorBody {
	return ErrorBody{Error: message, Code: code, RequestID: RequestIDFromGin(c)}
}

// NoRoute answers unmatched paths in the error shape. Without it Gin replies
// `404 page not found` as text/plain, which breaks any client that parses every
// response as JSON — including the generated ones.
func NoRoute() gin.HandlerFunc {
	return func(c *gin.Context) {
		Abort(c, http.StatusNotFound, CodeNotFound, "no such endpoint")
	}
}

// NoMethod answers a known path with an unsupported method.
//
// Requires gin.Engine.HandleMethodNotAllowed = true, otherwise Gin routes these
// to NoRoute and reports 404 for what is really a 405. Gin fills in the Allow
// header itself before this handler runs.
func NoMethod() gin.HandlerFunc {
	return func(c *gin.Context) {
		Abort(c, http.StatusMethodNotAllowed, CodeMethodNotAllowed,
			"method not allowed for this endpoint")
	}
}
