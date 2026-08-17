package infrastructure

import (
	"errors"
	"fmt"
	"net"
	"strings"

	"github.com/jackc/pgx/v5/pgconn"
)

// AllowInsecureDBEnv permits a plaintext connection to a host that is not local.
//
// Named for what it does rather than for an environment. A CAREGRAPH_ENV switch
// was considered and rejected — see the E4-S1 story — because a named
// environment invites unrelated behaviour to hang off it, and this is one
// decision about one connection.
//
// The Python pipelines read the same variable; see pipelines/common/dsn.py.
const AllowInsecureDBEnv = "CAREGRAPH_ALLOW_INSECURE_DB"

// ErrInsecureDSN is returned when the DSN would carry credentials and queries
// over a network in the clear.
var ErrInsecureDSN = errors.New("refusing an unencrypted database connection to a remote host")

// checkDSNTransport rejects a DSN that permits an unencrypted connection to a
// host that is not on this machine.
//
// `sslmode=disable` is correct for a loopback connection and for a single-host
// container network, and wrong for anything crossing a network — but nothing
// complains, the traffic is simply readable. The default is worse than disable:
// an unset sslmode means libpq's `prefer`, which attempts TLS and then falls
// back to plaintext *silently*, so the connection that was encrypted in staging
// may not be in production.
//
// Rather than matching on the sslmode string, this asks pgx what the DSN
// actually resolves to. That accounts for things a text comparison misses: the
// PG* environment variables, a service file, and — the reason it matters —
// `prefer`, which pgx represents as a TLS attempt plus a *plaintext fallback*.
// A DSN is insecure here if any connection it permits is unencrypted.
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

	// Every host:TLS pair the DSN allows — the primary plus each fallback that
	// pgx would try in turn.
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

// isLocalHost reports whether connecting to host stays on this machine.
//
// A Docker service name is deliberately *not* local: from inside the process
// there is no way to distinguish a private bridge network from the open
// internet, so compose sets the override instead.
func isLocalHost(host string) bool {
	host = strings.TrimSpace(host)

	// Empty host or an absolute path is a Unix socket — no network involved.
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
