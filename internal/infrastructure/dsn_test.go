package infrastructure

import (
	"errors"
	"strings"
	"testing"
)

// TestLocalPlaintextIsAllowed — sslmode=disable is the right setting for a
// loopback connection, and docker-compose depends on it.
func TestLocalPlaintextIsAllowed(t *testing.T) {
	for _, dsn := range []string{
		"postgres://u:p@localhost:5433/caregraph?sslmode=disable",
		"postgres://u:p@127.0.0.1:5433/caregraph?sslmode=disable",
		"postgres://u:p@[::1]:5433/caregraph?sslmode=disable",
		// No host at all: the Unix socket.
		"postgres:///caregraph?sslmode=disable",
		// libpq keyword form, and an explicit socket directory.
		"host=/var/run/postgresql dbname=caregraph",
		"host=localhost port=5433 dbname=caregraph sslmode=disable",
	} {
		t.Run(dsn, func(t *testing.T) {
			if err := checkDSNTransport(dsn, false); err != nil {
				t.Errorf("rejected a local connection: %v", err)
			}
		})
	}
}

// TestRemotePlaintextIsRejected covers the case the guard exists for.
//
// The DSN with no sslmode at all is the important one: libpq defaults to
// `prefer`, which pgx represents as a TLS attempt plus a plaintext *fallback*.
// A guard that only compared the sslmode string would let it through, and the
// connection would silently be in the clear whenever the server declined TLS.
func TestRemotePlaintextIsRejected(t *testing.T) {
	for _, dsn := range []string{
		"postgres://u:p@db.example.org:5432/caregraph?sslmode=disable",
		"postgres://u:p@db.example.org:5432/caregraph?sslmode=allow",
		"postgres://u:p@db.example.org:5432/caregraph?sslmode=prefer",
		"postgres://u:p@db.example.org:5432/caregraph",
		"host=db.example.org dbname=caregraph",
		// A Docker service name: not distinguishable from a public host here.
		"postgres://u:p@db:5432/caregraph?sslmode=disable",
		// Loopback primary, remote plaintext fallback — the multi-host form.
		"postgres://u:p@localhost,db.example.org/caregraph?sslmode=prefer",
	} {
		t.Run(dsn, func(t *testing.T) {
			err := checkDSNTransport(dsn, false)
			if err == nil {
				t.Fatal("accepted an unencrypted connection to a remote host")
			}
			if !errors.Is(err, ErrInsecureDSN) {
				t.Errorf("error should be ErrInsecureDSN, got %v", err)
			}
			if !strings.Contains(err.Error(), AllowInsecureDBEnv) {
				t.Error("the message should name the override, or the operator has to guess")
			}
		})
	}
}

func TestRemoteWithTLSIsAllowed(t *testing.T) {
	for _, mode := range []string{"require", "verify-ca", "verify-full"} {
		t.Run(mode, func(t *testing.T) {
			dsn := "postgres://u:p@db.example.org:5432/caregraph?sslmode=" + mode
			if err := checkDSNTransport(dsn, false); err != nil {
				t.Errorf("rejected a TLS connection: %v", err)
			}
		})
	}
}

// TestOverrideAllowsRemotePlaintext — compose sets this, because the API reaches
// the database over a private bridge network on one host.
func TestOverrideAllowsRemotePlaintext(t *testing.T) {
	dsn := "postgres://u:p@db:5432/caregraph?sslmode=disable"
	if err := checkDSNTransport(dsn, true); err != nil {
		t.Errorf("override did not take effect: %v", err)
	}
}

// TestUnparseableIsRejected: with no host to inspect there is nothing to
// conclude, and the safe conclusion is not "allow".
func TestUnparseableIsRejected(t *testing.T) {
	if err := checkDSNTransport("this is not = a dsn '", false); err == nil {
		t.Error("an unparseable DSN was accepted")
	}
}

func TestLoadConfigRejectsAnInsecureDSN(t *testing.T) {
	t.Setenv("DATABASE_URL", "postgres://u:p@db.example.org:5432/caregraph?sslmode=disable")
	t.Setenv(AllowInsecureDBEnv, "")

	if _, err := LoadConfig(); !errors.Is(err, ErrInsecureDSN) {
		t.Errorf("LoadConfig error = %v, want ErrInsecureDSN", err)
	}
}

func TestLoadConfigStillRequiresADSN(t *testing.T) {
	t.Setenv("DATABASE_URL", "")

	if _, err := LoadConfig(); !errors.Is(err, ErrNoDatabaseURL) {
		t.Errorf("LoadConfig error = %v, want ErrNoDatabaseURL", err)
	}
}
