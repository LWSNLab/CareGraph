// Package auth provides API-key authentication and (later) rate limiting for
// the public gateway. See docs/architecture/security.md §2.
package auth

import (
	"net/http"

	"github.com/LWSNLab/caregraph/internal/infrastructure"
	"github.com/gin-gonic/gin"
)

// APIKeyMiddleware validates the X-API-Key header.
//
// TODO: verify the key against Argon2id hashes stored in Postgres and apply a
// Redis-backed token-bucket rate limit (cfg.RedisAddr). For now it only checks
// that a key is present, so the skeleton runs end-to-end.
func APIKeyMiddleware(cfg infrastructure.Config) gin.HandlerFunc {
	_ = cfg // reserved for the hashed-key store and rate limiter
	return func(c *gin.Context) {
		if c.GetHeader("X-API-Key") == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "Unauthorized: Invalid API Key provided",
			})
			return
		}
		c.Next()
	}
}
