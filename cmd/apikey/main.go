// Command apikey issues, lists and revokes API keys (story E3-S4).
//
// Separate from the gateway on purpose: the gateway's database role has SELECT
// on api_key and nothing more, so a compromised gateway cannot mint itself a key
// or erase an audit trail. This tool connects as the owner.
//
//	go run ./cmd/apikey issue --name "Acme GmbH" --tier enterprise
//	go run ./cmd/apikey list
//	go run ./cmd/apikey revoke --key-id 3f9a2b1c4d5e6f70
//
// The plaintext key is printed once and never stored. There is no recovery path;
// a lost key is revoked and reissued.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/LWSNLab/caregraph/internal/auth"
	"github.com/jackc/pgx/v5/pgxpool"
)

const defaultDSN = "postgres://caregraph:caregraph@localhost:5433/caregraph?sslmode=disable"

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: apikey <issue|list|revoke> [flags]")
	}

	fs := flag.NewFlagSet(args[0], flag.ExitOnError)
	dsn := fs.String("dsn", env("ADMIN_DATABASE_URL", defaultDSN),
		"Postgres DSN of an owner-level role (defaults to $ADMIN_DATABASE_URL)")
	name := fs.String("name", "", "who the key is issued to (issue)")
	tier := fs.String("tier", string(auth.TierCommunity), "community|enterprise (issue)")
	limit := fs.Int("limit-per-min", 0, "per-key override for the tier default (issue)")
	keyID := fs.String("key-id", "", "public key id (revoke)")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, *dsn)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		return fmt.Errorf("ping: %w", err)
	}

	switch args[0] {
	case "issue":
		return issue(ctx, pool, *name, *tier, *limit)
	case "list":
		return list(ctx, pool)
	case "revoke":
		return revoke(ctx, pool, *keyID)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func issue(ctx context.Context, pool *pgxpool.Pool, name, tier string, limit int) error {
	if name == "" {
		return errors.New("--name is required: an unattributable key cannot be revoked with confidence")
	}
	if tier != string(auth.TierCommunity) && tier != string(auth.TierEnterprise) {
		return fmt.Errorf("unknown tier %q (community|enterprise)", tier)
	}

	plaintext, keyID, hash, err := auth.GenerateKey()
	if err != nil {
		return err
	}

	var override *int
	if limit > 0 {
		override = &limit
	}

	_, err = pool.Exec(ctx, `
		INSERT INTO api_key (key_id, secret_hash, name, tier, rate_limit_per_min)
		VALUES ($1, $2, $3, $4::api_tier, $5)`,
		keyID, hash, name, tier, override)
	if err != nil {
		return fmt.Errorf("insert key: %w", err)
	}

	effective := limit
	if effective == 0 {
		effective = auth.DefaultLimitPerMin[auth.Tier(tier)]
	}

	// stdout so the key can be piped; everything explanatory goes to stderr.
	fmt.Fprintf(os.Stderr,
		"issued to %q — tier %s, %d req/min, key id %s\n"+
			"This is the only time the key is shown. Store it now.\n\n",
		name, tier, effective, keyID)
	fmt.Println(plaintext)
	return nil
}

func list(ctx context.Context, pool *pgxpool.Pool) error {
	rows, err := pool.Query(ctx, `
		SELECT key_id, name, tier::text, rate_limit_per_min, created_at, revoked_at
		  FROM api_key ORDER BY created_at`)
	if err != nil {
		return fmt.Errorf("list keys: %w", err)
	}
	defer rows.Close()

	fmt.Printf("%-18s %-28s %-11s %-9s %-12s %s\n",
		"KEY ID", "NAME", "TIER", "REQ/MIN", "CREATED", "STATE")
	for rows.Next() {
		var (
			keyID, name, tier string
			override          *int
			created           time.Time
			revoked           *time.Time
		)
		if err := rows.Scan(&keyID, &name, &tier, &override, &created, &revoked); err != nil {
			return fmt.Errorf("scan key: %w", err)
		}

		perMin := auth.DefaultLimitPerMin[auth.Tier(tier)]
		if override != nil {
			perMin = *override
		}
		state := "active"
		if revoked != nil {
			state = "revoked " + revoked.Format("2006-01-02")
		}
		fmt.Printf("%-18s %-28s %-11s %-9d %-12s %s\n",
			keyID, truncate(name, 28), tier, perMin, created.Format("2006-01-02"), state)
	}
	return rows.Err()
}

func revoke(ctx context.Context, pool *pgxpool.Pool, keyID string) error {
	if keyID == "" {
		return errors.New("--key-id is required")
	}
	// Idempotent, and it never clears an existing revocation date: the first time
	// a key stopped being valid is the fact an incident review needs.
	tag, err := pool.Exec(ctx,
		`UPDATE api_key SET revoked_at = now() WHERE key_id = $1 AND revoked_at IS NULL`, keyID)
	if err != nil {
		return fmt.Errorf("revoke: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return fmt.Errorf("no active key with id %q", keyID)
	}
	fmt.Fprintf(os.Stderr, "revoked %s\n", keyID)
	fmt.Fprintf(os.Stderr,
		"Note: the gateway caches verifications for up to a minute, so the key may "+
			"still work briefly.\n")
	return nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
