package provider

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
)

// Handler exposes the provider domain over HTTP (Gin).
type Handler struct {
	repo Repository
}

// NewHandler wires HTTP handlers over a Repository.
func NewHandler(repo Repository) *Handler {
	return &Handler{repo: repo}
}

// Health is the liveness/readiness probe (GET /healthz).
func (h *Handler) Health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

// Near handles GET /v1/infrastructure/near — spatial radius search.
func (h *Handler) Near(c *gin.Context) {
	lat, err1 := strconv.ParseFloat(c.Query("lat"), 64)
	lng, err2 := strconv.ParseFloat(c.Query("lng"), 64)
	if err1 != nil || err2 != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid latitude or longitude value"})
		return
	}

	radiusKm := queryFloat(c, "radius_km", 10.0)
	limit := queryInt(c, "limit", 20)

	var t *Type
	if v := c.Query("type"); v != "" {
		pt := Type(v)
		t = &pt
	}

	results, err := h.repo.Near(c.Request.Context(), lat, lng, radiusKm, t, limit)
	if err != nil {
		respondRepoErr(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"total": len(results), "data": results})
}

// Search handles GET /v1/infrastructure/search — fuzzy text search (Typesense).
// TODO: wire internal/search.Client.
func (h *Handler) Search(c *gin.Context) {
	if len(c.Query("q")) < 2 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "query parameter 'q' must be at least 2 characters"})
		return
	}
	c.JSON(http.StatusNotImplemented, gin.H{"error": "search not implemented"})
}

// GetByIK handles GET /v1/infrastructure/:ik_nummer.
func (h *Handler) GetByIK(c *gin.Context) {
	p, err := h.repo.GetByIK(c.Request.Context(), c.Param("ik_nummer"))
	if err != nil {
		respondRepoErr(c, err)
		return
	}
	if p == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "Provider not found"})
		return
	}
	c.JSON(http.StatusOK, p)
}

func respondRepoErr(c *gin.Context, err error) {
	if errors.Is(err, ErrNotImplemented) {
		c.JSON(http.StatusNotImplemented, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusInternalServerError, gin.H{"error": "internal error"})
}

func queryFloat(c *gin.Context, key string, def float64) float64 {
	if v, err := strconv.ParseFloat(c.Query(key), 64); err == nil {
		return v
	}
	return def
}

func queryInt(c *gin.Context, key string, def int) int {
	if v, err := strconv.Atoi(c.Query(key)); err == nil {
		return v
	}
	return def
}
