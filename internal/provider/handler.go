package provider

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/search"
	"github.com/gin-gonic/gin"
)

// Handler exposes the provider domain over HTTP (Gin).
type Handler struct {
	repo   Repository
	search search.Client
	log    *slog.Logger
}

// NewHandler wires HTTP handlers over a Repository, logging to slog.Default().
//
// The search client is optional: without one `/search` answers 501, which is
// what it did before the engine existed. That keeps the gateway runnable for
// anyone who does not want to operate Typesense.
func NewHandler(repo Repository) *Handler {
	return &Handler{repo: repo, log: slog.Default()}
}

// WithSearch returns a copy of h that serves /search from the given engine.
func (h *Handler) WithSearch(client search.Client) *Handler {
	c := *h
	c.search = client
	return &c
}

// WithLogger returns a copy of h that logs to l.
func (h *Handler) WithLogger(l *slog.Logger) *Handler {
	c := *h
	c.log = l
	return &c
}

// Near handles GET /v1/infrastructure/near — spatial radius search.
func (h *Handler) Near(c *gin.Context) {
	params, err := ParseNearParams(c.Request.URL.Query())
	if err != nil {
		httpx.Fail(c, http.StatusBadRequest, httpx.CodeInvalidParameter, err.Error())
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
	c.JSON(http.StatusOK, ListResponse{Total: len(results), Data: results})
}

// Search handles GET /v1/infrastructure/search — typo-tolerant text search.
//
// Two stages: the engine ranks, Postgres fills in. The index holds a subset of
// the fields, so answering from it directly would give this endpoint a different
// response shape from /near; hydrating keeps one schema and one source of truth.
func (h *Handler) Search(c *gin.Context) {
	params, err := ParseSearchParams(c.Request.URL.Query())
	if err != nil {
		httpx.Fail(c, http.StatusBadRequest, httpx.CodeInvalidParameter, err.Error())
		return
	}

	if h.search == nil {
		httpx.Fail(c, http.StatusNotImplemented, httpx.CodeNotImplemented,
			"search is not configured on this deployment")
		return
	}

	query := search.Query{Text: params.Query, City: params.City, Limit: params.Limit}
	if params.Type != nil {
		query.Type = string(*params.Type)
	}

	hits, err := h.search.Search(c.Request.Context(), query)
	if err != nil {
		h.respondSearchErr(c, err)
		return
	}

	results, err := h.repo.BySourceIDs(c.Request.Context(), hits.IDs)
	if err != nil {
		h.respondRepoErr(c, err)
		return
	}
	if results == nil {
		results = []Provider{}
	}

	// `total` is the engine's match count, not len(data): it is what a client
	// needs to know there is more behind the limit.
	c.JSON(http.StatusOK, ListResponse{Total: hits.Found, Data: results})
}

// respondSearchErr maps an engine failure. Unreachable is 503, not 500: nothing
// is broken in the request or in this service, the feature is temporarily
// impossible and retrying is the right response.
func (h *Handler) respondSearchErr(c *gin.Context, err error) {
	ctx := c.Request.Context()
	log := httpx.Logger(h.log, c).With(
		"path", c.Request.URL.Path, "query", c.Request.URL.RawQuery, "error", err)

	switch {
	case errors.Is(err, context.Canceled) && ctx.Err() != nil:
		log.DebugContext(ctx, "client disconnected during search")
		c.AbortWithStatus(httpx.StatusClientClosedRequest)

	case errors.Is(err, context.DeadlineExceeded):
		log.ErrorContext(ctx, "search timed out")
		httpx.Fail(c, http.StatusGatewayTimeout, httpx.CodeTimeout,
			"the search took too long, please retry")

	case errors.Is(err, search.ErrUnavailable):
		log.ErrorContext(ctx, "search engine unavailable")
		httpx.Fail(c, http.StatusServiceUnavailable, httpx.CodeUnavailable,
			"search is temporarily unavailable, please retry")

	default:
		log.ErrorContext(ctx, "search failed")
		httpx.Fail(c, http.StatusInternalServerError, httpx.CodeInternal, "internal error")
	}
}

// GetByIK handles GET /v1/infrastructure/:ik_nummer.
func (h *Handler) GetByIK(c *gin.Context) {
	ik := c.Param("ik_nummer")
	if err := ValidateIK(ik); err != nil {
		httpx.Fail(c, http.StatusBadRequest, httpx.CodeInvalidParameter, err.Error())
		return
	}

	p, err := h.repo.GetByIK(c.Request.Context(), ik)
	if err != nil {
		h.respondRepoErr(c, err)
		return
	}
	if p == nil {
		httpx.Fail(c, http.StatusNotFound, httpx.CodeNotFound,
			"No institution with this IK number")
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
	log := httpx.Logger(h.log, c).With(
		"method", c.Request.Method,
		"path", c.Request.URL.Path,
		"query", c.Request.URL.RawQuery,
		"error", err,
	)

	switch {
	case errors.Is(err, ErrNotImplemented):
		httpx.Fail(c, http.StatusNotImplemented, httpx.CodeNotImplemented, err.Error())

	// The caller hung up mid-request. Nothing failed here and there is nobody
	// left to answer, so this must not be counted as a server error.
	case errors.Is(err, context.Canceled) && ctx.Err() != nil:
		log.DebugContext(ctx, "client disconnected before the response was ready")
		c.AbortWithStatus(httpx.StatusClientClosedRequest)

	// Our own queryTimeout, the request-scoped timeout, or one inherited from
	// the caller. Distinct from a generic 500 because it is worth retrying and
	// points at the database rather than at the request.
	case errors.Is(err, context.DeadlineExceeded):
		log.ErrorContext(ctx, "database query timed out")
		httpx.Fail(c, http.StatusGatewayTimeout, httpx.CodeTimeout,
			"the query took too long, please retry")

	default:
		log.ErrorContext(ctx, "request failed")
		httpx.Fail(c, http.StatusInternalServerError, httpx.CodeInternal, "internal error")
	}
}
