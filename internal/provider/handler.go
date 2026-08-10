package provider

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
)

// clientClosedRequest is nginx's 499: the caller went away before the response
// was written. Not in net/http because it is not an IANA-registered code.
const clientClosedRequest = 499

// Handler exposes the provider domain over HTTP (Gin).
type Handler struct {
	repo Repository
	log  *slog.Logger
}

// NewHandler wires HTTP handlers over a Repository, logging to slog.Default().
func NewHandler(repo Repository) *Handler {
	return &Handler{repo: repo, log: slog.Default()}
}

// WithLogger returns a copy of h that logs to l.
func (h *Handler) WithLogger(l *slog.Logger) *Handler {
	c := *h
	c.log = l
	return &c
}

// Health is the liveness/readiness probe (GET /healthz).
func (h *Handler) Health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

// Near handles GET /v1/infrastructure/near — spatial radius search.
func (h *Handler) Near(c *gin.Context) {
	params, err := ParseNearParams(c.Request.URL.Query())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	results, err := h.repo.Near(c.Request.Context(), params)
	if err != nil {
		h.respondRepoErr(c, err)
		return
	}
	if results == nil {
		results = []Provider{}
	}
	c.JSON(http.StatusOK, gin.H{"total": len(results), "data": results})
}

// Search handles GET /v1/infrastructure/search — fuzzy text search (Typesense).
// TODO: wire internal/search.Client (E3-S2).
func (h *Handler) Search(c *gin.Context) {
	if len(c.Query("q")) < 2 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "query parameter 'q' must be at least 2 characters"})
		return
	}
	c.JSON(http.StatusNotImplemented, gin.H{"error": "search not implemented"})
}

// GetByIK handles GET /v1/infrastructure/:ik_nummer.
func (h *Handler) GetByIK(c *gin.Context) {
	ik := c.Param("ik_nummer")
	if err := ValidateIK(ik); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	p, err := h.repo.GetByIK(c.Request.Context(), ik)
	if err != nil {
		h.respondRepoErr(c, err)
		return
	}
	if p == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "No institution with this IK number"})
		return
	}
	c.JSON(http.StatusOK, p)
}

// respondRepoErr turns a repository failure into a response.
//
// Two rules it enforces. The driver error never reaches the client — it can
// carry the DSN, the query and column names. And it is never dropped either:
// a 500 whose cause was not written anywhere leaves an operator with a status
// code and nothing else.
func (h *Handler) respondRepoErr(c *gin.Context, err error) {
	ctx := c.Request.Context()
	attrs := []any{
		"method", c.Request.Method,
		"path", c.Request.URL.Path,
		"query", c.Request.URL.RawQuery,
		"error", err,
	}

	switch {
	case errors.Is(err, ErrNotImplemented):
		c.JSON(http.StatusNotImplemented, gin.H{"error": err.Error()})

	// The caller hung up mid-request. Nothing failed here and there is nobody
	// left to answer, so this must not be counted as a server error.
	case errors.Is(err, context.Canceled) && ctx.Err() != nil:
		h.log.DebugContext(ctx, "client disconnected before the response was ready", attrs...)
		// 499 is nginx's "Client Closed Request". Non-standard, and nobody is
		// there to read it — but it keeps abandoned requests from showing up as
		// successes in the access log.
		c.AbortWithStatus(clientClosedRequest)

	// Our own queryTimeout, or one inherited from the caller. Distinct from a
	// generic 500 because it is worth retrying and points at the database
	// rather than at the request.
	case errors.Is(err, context.DeadlineExceeded):
		h.log.ErrorContext(ctx, "database query timed out", attrs...)
		c.JSON(http.StatusGatewayTimeout, gin.H{"error": "the query took too long, please retry"})

	default:
		h.log.ErrorContext(ctx, "request failed", attrs...)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "internal error"})
	}
}
