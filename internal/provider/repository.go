package provider

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// queryTimeout bounds a single database round trip. Measured p95 is under
// 10 ms, so this is a backstop, not a budget: without it a stalled database
// holds the request open and keeps a pool connection checked out indefinitely,
// and enough of those exhaust the pool for every other caller.
const queryTimeout = 5 * time.Second

// Repository is the persistence port for care-infrastructure queries.
type Repository interface {
	// Near returns providers within p.RadiusKm of (p.Lat, p.Lng), nearest first.
	Near(ctx context.Context, p NearParams) ([]Provider, error)
	// GetByIK returns a single provider by its Institutionskennzeichen.
	GetByIK(ctx context.Context, ik string) (*Provider, error)
}

// PostgresRepository implements Repository against PostgreSQL/PostGIS.
type PostgresRepository struct {
	pool *pgxpool.Pool
}

// NewPostgresRepository wires a repository over the given pgx pool.
func NewPostgresRepository(pool *pgxpool.Pool) *PostgresRepository {
	return &PostgresRepository{pool: pool}
}

// providerColumns is the column list every provider query selects, in the order
// scanProvider expects.
const providerColumns = `id::text, ik_nummer, type::text, name,
       parent_organization, website, strasse, plz, ort, bundesland, details`

// originPoint builds the search origin. It is repeated inline rather than
// hoisted into a CTE: a single-row CTE joined to the table can stop the planner
// from treating the origin as a constant, which loses the GIST index.
const originPoint = `ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography`

// nearQuery filters with ST_DWithin (index-assisted) and orders by true
// distance. $3 is the radius in metres — ST_DWithin on geography works in
// metres, not degrees.
//
// The type filter compares against a text parameter cast to the enum in SQL, so
// pgx never has to encode the custom provider_type OID itself.
//
// The output distance is rounded to metres — the raw value carries eleven
// decimal places, advertising sub-nanometre accuracy for coordinates good to
// about ten metres. Ordering uses the unrounded distance, with id as a
// tiebreaker so that repeating a request with a LIMIT returns the same rows in
// the same order.
var nearQuery = fmt.Sprintf(`
SELECT %s,
       round((ST_Distance(location, %s) / 1000.0)::numeric, 3)::double precision AS distance_km
FROM   care_infrastructure
WHERE  location IS NOT NULL
  AND  ST_DWithin(location, %s, $3)
  AND  ($4::text IS NULL OR type = $4::provider_type)
ORDER  BY ST_Distance(location, %s), id
LIMIT  $5`, providerColumns, originPoint, originPoint, originPoint)

// Near implements the radius search behind GET /v1/infrastructure/near.
func (r *PostgresRepository) Near(ctx context.Context, p NearParams) ([]Provider, error) {
	ctx, cancel := context.WithTimeout(ctx, queryTimeout)
	defer cancel()

	var typeFilter *string
	if p.Type != nil {
		s := string(*p.Type)
		typeFilter = &s
	}

	rows, err := r.pool.Query(ctx, nearQuery,
		p.Lng, p.Lat, p.RadiusKm*1000, typeFilter, p.Limit)
	if err != nil {
		return nil, fmt.Errorf("near query: %w", err)
	}
	defer rows.Close()

	// Non-nil so the handler serialises an empty result as [] rather than null.
	results := make([]Provider, 0, p.Limit)
	for rows.Next() {
		var (
			prov     Provider
			distance float64
		)
		if err := scanProvider(rows, &prov, &distance); err != nil {
			return nil, err
		}
		prov.DistanceKm = &distance
		results = append(results, prov)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("near rows: %w", err)
	}
	return results, nil
}

// getByIKQuery is an index lookup on the unique ik_nummer. No distance column:
// the entity is addressed directly, not found in relation to a point.
const getByIKQuery = `
SELECT ` + providerColumns + `
FROM   care_infrastructure
WHERE  ik_nummer = $1`

// GetByIK implements GET /v1/infrastructure/:ik_nummer. A missing row is
// (nil, nil) — absence is a valid answer to a lookup, not a failure.
func (r *PostgresRepository) GetByIK(ctx context.Context, ik string) (*Provider, error) {
	ctx, cancel := context.WithTimeout(ctx, queryTimeout)
	defer cancel()

	var p Provider
	err := scanProvider(r.pool.QueryRow(ctx, getByIKQuery, ik), &p)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		return nil, nil
	case err != nil:
		return nil, fmt.Errorf("lookup by ik %q: %w", ik, err)
	}
	return &p, nil
}

// scanProvider reads providerColumns into dst. Any extra targets are scanned
// after them, in order — Near uses that for its distance column.
// Everything except type, name and details is nullable in the schema.
func scanProvider(row pgx.Row, dst *Provider, extra ...any) error {
	var (
		typeStr                                     string
		ik, parent, website, street, plz, ort, land *string
	)
	targets := []any{
		&dst.ID, &ik, &typeStr, &dst.Name,
		&parent, &website, &street, &plz, &ort, &land,
		&dst.Details,
	}
	if err := row.Scan(append(targets, extra...)...); err != nil {
		return fmt.Errorf("scan provider: %w", err)
	}

	dst.Type = Type(typeStr)
	dst.IKNummer = ik
	dst.ParentOrganization = parent
	dst.Website = website
	dst.Address = Address{
		Street:     deref(street),
		PostalCode: deref(plz),
		City:       deref(ort),
		State:      deref(land),
	}
	return nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// Ensure PostgresRepository satisfies Repository at compile time.
var _ Repository = (*PostgresRepository)(nil)
