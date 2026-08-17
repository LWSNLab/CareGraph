package infrastructure

import (
	"errors"
	"fmt"
	"net"
	"strings"

	"github.com/jackc/pgx/v5/pgconn"
)

// AllowInsecureDBEnv permits a plaintext connection to a non-local host. Named
// for what it does rather than for an environment, so nothing unrelated hangs off
// it. The Python pipelines read the same variable — pipelines/common/dsn.py.
const AllowInsecureDBEnv = "CAREGRAPH_ALLOW_INSECURE_DB"

// ErrInsecureDSN is returned when the DSN would carry credentials and queries
// over a network in the clear.
var ErrInsecureDSN = errors.New("refusing an unencrypted database connection to a remote host")

// checkDSNTransport rejects a DSN that permits an unencrypted connection to a
// host that is not on this machine.
//
// An unset sslmode is the dangerous case, not `disable`: libpq treats it as
// `prefer`, which attempts TLS and then falls back to plaintext silently. So this
// asks pgx what the DSN resolves to rather than matching the sslmode string —
// pgx represents `prefer` as a TLS attempt plus a plaintext *fallback*, and that
// is what a text comparison misses. Insecure if any connection it permits is.
func checkDSNTransport(dsn string, allowInsecure bool) error {
	if allowInsecure {
		return nil
	}

	cfg, err := pgconn.ParseConfig(dsn)
	if err != nil {
		// Unparseable is not a pass: with no host to inspect, the safe
		// assumption is that it is remote.
		return fmt.Errorf("cannot parse DATABASE_URL: %w", err)
	}

	// Every host:TLS pair the DSN allows: the primary plus each pgx fallback.
	type attempt struct {
		host  string
		plain bool
	}
	attempts := []attempt{{host: cfg.Host, plain: cfg.TLSConfig == nil}}
	for _, fb := range cfg.Fallbacks {
		host := fb.Host
		if host == "" {
			host = cfg.Host
		}
		attempts = append(attempts, attempt{host: host, plain: fb.TLSConfig == nil})
	}

	for _, a := range attempts {
		if a.plain && !isLocalHost(a.host) {
			return fmt.Errorf(
				"%w: %q would be reached without TLS. Use sslmode=require, or "+
					"verify-ca / verify-full to check the certificate as well. If this "+
					"host really is local — a container network on one machine, say — "+
					"set %s=1",
				ErrInsecureDSN, a.host, AllowInsecureDBEnv)
		}
	}
	return nil
}

// isLocalHost reports whether connecting to host stays on this machine. A Docker
// service name is deliberately not local — from inside the process a private
// bridge network is indistinguishable from the internet — so compose sets the
// override instead.
func isLocalHost(host string) bool {
	host = strings.TrimSpace(host)

	// Empty host or an absolute path is a Unix socket.
	if host == "" || strings.HasPrefix(host, "/") {
		return true
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	if ip := net.ParseIP(strings.Trim(host, "[]")); ip != nil {
		return ip.IsLoopback()
	}
	return false
}
