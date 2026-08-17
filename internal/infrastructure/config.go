// Package infrastructure holds cross-cutting wiring: configuration and the
// database/PostGIS connection. It has no domain logic of its own.
package infrastructure

import (
	"errors"
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

// ErrNoDatabaseURL is returned when DATABASE_URL is unset.
var ErrNoDatabaseURL = errors.New(
	"DATABASE_URL is not set — copy .env.example to .env, or pass the DSN explicitly")

// LoadConfig reads configuration from the environment.
//
// Addresses and URLs have defaults that match docker-compose.yml, because
// getting one wrong produces a connection error that says so. **DATABASE_URL
// has none**: it is the only setting that carries a credential, and a default
// credential is one that gets deployed. `caregraph:caregraph` sat here as a
// fallback, which meant a misspelt environment variable in production did not
// fail — it silently looked for a database with the development password.
//
// The dev value now lives in .env.example, where it is visibly an example.
func LoadConfig() (Config, error) {
	dsn := os.Getenv("DATABASE_URL")
	if strings.TrimSpace(dsn) == "" {
		return Config{}, ErrNoDatabaseURL
	}

	return Config{
		HTTPAddr:     env("CAREGRAPH_HTTP_ADDR", ":8080"),
		DatabaseURL:  dsn,
		TypesenseURL: env("TYPESENSE_URL", "http://localhost:8108"),
		TypesenseKey: env("TYPESENSE_API_KEY", ""),
		RedisAddr:    env("REDIS_ADDR", "localhost:6379"),
		LogLevel:     logLevel(env("CAREGRAPH_LOG_LEVEL", "info")),
	}, nil
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
