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

	// TrustedProxies are the addresses or CIDRs whose X-Forwarded-For header may
	// be believed. Empty means trust none, so ClientIP() reports the peer that
	// actually connected.
	TrustedProxies []string
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

	// Before the pool is opened, so a misconfigured deployment fails at startup
	// rather than sending credentials over a network in the clear.
	allowInsecure := strings.TrimSpace(os.Getenv(AllowInsecureDBEnv)) != ""
	if err := checkDSNTransport(dsn, allowInsecure); err != nil {
		return Config{}, err
	}

	return Config{
		HTTPAddr:       env("CAREGRAPH_HTTP_ADDR", ":8080"),
		DatabaseURL:    dsn,
		TypesenseURL:   env("TYPESENSE_URL", "http://localhost:8108"),
		TypesenseKey:   env("TYPESENSE_API_KEY", ""),
		RedisAddr:      env("REDIS_ADDR", "localhost:6379"),
		LogLevel:       logLevel(env("CAREGRAPH_LOG_LEVEL", "info")),
		TrustedProxies: trustedProxies(os.Getenv("CAREGRAPH_TRUSTED_PROXIES")),
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

// trustedProxies parses the comma-separated CAREGRAPH_TRUSTED_PROXIES list.
//
// Empty by default, and deliberately so: trusting X-Forwarded-For from anyone
// lets a client spoof its own address and walk around the per-address
// failed-authentication budget. Set it only to the reverse proxy in front, and
// only to that.
//
// It has to be set once there *is* a proxy, or the opposite failure appears:
// ClientIP() returns the proxy's address for every request, so all clients share
// one rate-limit bucket and one failed-auth budget.
func trustedProxies(raw string) []string {
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}
