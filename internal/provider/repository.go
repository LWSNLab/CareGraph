package provider

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Repository is the persistence port for care-infrastructure queries.
type Repository interface {
	// Near returns providers within radiusKm of (lat, lng), ordered by distance.
	Near(ctx context.Context, lat, lng, radiusKm float64, t *Type, limit int) ([]Provider, error)
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

// Near — TODO: implement with ST_DWithin + ST_Distance ordering.
// See docs/architecture/data-schema.md §5 for the reference query.
func (r *PostgresRepository) Near(ctx context.Context, lat, lng, radiusKm float64, t *Type, limit int) ([]Provider, error) {
	return nil, ErrNotImplemented
}

// GetByIK — TODO: SELECT ... FROM care_infrastructure WHERE ik_nummer = $1.
func (r *PostgresRepository) GetByIK(ctx context.Context, ik string) (*Provider, error) {
	return nil, ErrNotImplemented
}

// Ensure PostgresRepository satisfies Repository at compile time.
var _ Repository = (*PostgresRepository)(nil)
