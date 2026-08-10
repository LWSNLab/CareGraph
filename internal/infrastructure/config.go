// Package infrastructure holds cross-cutting wiring: configuration and the
// database/PostGIS connection. It has no domain logic of its own.
package infrastructure

import (
	"log/slog"
	"os"
	"strings"
)

// Config holds runtime configuration, sourced from environment variables.
type Config struct {
	HTTPAddr     string     // address the API listens on, e.g. ":8080"
	DatabaseURL  string     // PostgreSQL/PostGIS DSN
	TypesenseURL string     // Typesense base URL
	TypesenseKey string     // Typesense API key
	RedisAddr    string     // Redis address for rate limiting
	LogLevel     slog.Level // minimum level written to stderr
}

// LoadConfig reads configuration from the environment, applying sane defaults
// that match docker-compose.yml for local development.
func LoadConfig() Config {
	return Config{
		HTTPAddr:     env("CAREGRAPH_HTTP_ADDR", ":8080"),
		DatabaseURL:  env("DATABASE_URL", "postgres://caregraph:caregraph@localhost:5433/caregraph?sslmode=disable"),
		TypesenseURL: env("TYPESENSE_URL", "http://localhost:8108"),
		TypesenseKey: env("TYPESENSE_API_KEY", ""),
		RedisAddr:    env("REDIS_ADDR", "localhost:6379"),
		LogLevel:     logLevel(env("CAREGRAPH_LOG_LEVEL", "info")),
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// logLevel maps a name to a slog level. An unrecognised value falls back to
// info rather than failing startup: a typo in a log setting should not keep the
// service down.
func logLevel(name string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
