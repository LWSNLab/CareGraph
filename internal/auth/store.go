package auth

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Tier names the request quota a key is entitled to.
type Tier string

const (
	TierCommunity  Tier = "community"
	TierEnterprise Tier = "enterprise"
)

// DefaultLimitPerMin per tier, from docs/architecture/security.md §2.
var DefaultLimitPerMin = map[Tier]int{
	TierCommunity:  100,
	TierEnterprise: 6000,
}

// Identity is what a verified key resolves to.
type Identity struct {
	KeyID       string
	Name        string
	Tier        Tier
	LimitPerMin int
}

// ErrNoSuchKey is returned when no active key carries the given id. Kept
// separate from a wrong secret only internally — the caller answers 401 for both,
// because telling them apart tells an attacker which key ids exist.
var ErrNoSuchKey = errors.New("no such api key")

// KeyStore verifies presented keys.
type KeyStore interface {
	Verify(ctx context.Context, presented string) (*Identity, error)
}

// PostgresKeyStore verifies against the api_key table, with a short-lived cache
// in front of Argon2id.
type PostgresKeyStore struct {
	pool *pgxpool.Pool
	ttl  time.Duration

	mu    sync.RWMutex
	cache map[[32]byte]cacheEntry
}

type cacheEntry struct {
	identity *Identity // nil for a known-bad key
	expires  time.Time
}

// cacheTTL bounds how long a revocation takes to take effect.
//
// Argon2id at 64 MiB costs tens of milliseconds — enough to dominate a p95 of
// ~9 ms and to make the endpoint slower than the query it serves. Caching the
// verification result keeps that off the hot path; the price is that a revoked
// key keeps working for up to this long. A minute is short enough to be an
// acceptable incident window and long enough that a busy client pays for
// Argon2id once rather than per request.
const cacheTTL = time.Minute

// NewPostgresKeyStore wires a key store over the given pool.
func NewPostgresKeyStore(pool *pgxpool.Pool) *PostgresKeyStore {
	return &PostgresKeyStore{
		pool:  pool,
		ttl:   cacheTTL,
		cache: make(map[[32]byte]cacheEntry),
	}
}

// Verify resolves a presented key to an identity, or an error.
//
// Both outcomes are cached: a valid key to avoid re-running Argon2id, and an
// invalid one so that repeatedly presenting a wrong secret for a *known* key id
// cannot be used to burn CPU. Only a syntactically valid key ever reaches the
// database.
func (s *PostgresKeyStore) Verify(ctx context.Context, presented string) (*Identity, error) {
	keyID, secret, err := SplitKey(presented)
	if err != nil {
		return nil, err
	}

	ck := cacheKey(presented)
	if entry, ok := s.lookupCache(ck); ok {
		if entry.identity == nil {
			return nil, ErrNoSuchKey
		}
		return entry.identity, nil
	}

	var (
		name, tier string
		override   *int
		storedHash string
		storedID   string
	)
	err = s.pool.QueryRow(ctx,
		`SELECT key_id, name, tier::text, rate_limit_per_min, secret_hash
		   FROM api_key WHERE key_id = $1 AND revoked_at IS NULL`, keyID,
	).Scan(&storedID, &name, &tier, &override, &storedHash)

	switch {
	case errors.Is(err, pgx.ErrNoRows):
		// Not cached: an unknown key id costs only an indexed lookup, and caching
		// it would let a flood of random ids evict real entries.
		return nil, ErrNoSuchKey
	case err != nil:
		return nil, fmt.Errorf("look up api key: %w", err)
	}

	ok, err := VerifySecret(secret, storedHash)
	if err != nil {
		return nil, fmt.Errorf("verify api key %s: %w", keyID, err)
	}
	if !ok {
		s.store(ck, cacheEntry{identity: nil, expires: time.Now().Add(s.ttl)})
		return nil, ErrNoSuchKey
	}

	identity := &Identity{
		KeyID:       storedID,
		Name:        name,
		Tier:        Tier(tier),
		LimitPerMin: limitFor(Tier(tier), override),
	}
	s.store(ck, cacheEntry{identity: identity, expires: time.Now().Add(s.ttl)})
	return identity, nil
}

func limitFor(tier Tier, override *int) int {
	if override != nil && *override > 0 {
		return *override
	}
	if limit, ok := DefaultLimitPerMin[tier]; ok {
		return limit
	}
	return DefaultLimitPerMin[TierCommunity]
}

func (s *PostgresKeyStore) lookupCache(ck [32]byte) (cacheEntry, bool) {
	s.mu.RLock()
	entry, ok := s.cache[ck]
	s.mu.RUnlock()
	if !ok || time.Now().After(entry.expires) {
		return cacheEntry{}, false
	}
	return entry, true
}

func (s *PostgresKeyStore) store(ck [32]byte, entry cacheEntry) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Bounded so a stream of wrong secrets for known ids cannot grow the map
	// without limit. Dropping everything is crude but correct: the cost of a
	// cold cache is one Argon2id per active key, not a correctness problem.
	if len(s.cache) > 4096 {
		s.cache = make(map[[32]byte]cacheEntry, 64)
	}
	s.cache[ck] = entry
}

// Ensure PostgresKeyStore satisfies KeyStore at compile time.
var _ KeyStore = (*PostgresKeyStore)(nil)
