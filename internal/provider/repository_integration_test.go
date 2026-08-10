package provider

import (
	"context"
	"math"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Integration tests for the PostGIS query. They need a real database and skip
// without one:
//
//	docker compose up -d db
//	CAREGRAPH_TEST_DSN=postgres://caregraph:caregraph@localhost:5433/caregraph?sslmode=disable \
//	    go test ./internal/provider/
//
// Fixtures are placed at (0°, 0°) in the Gulf of Guinea, far from any real
// German record, so the assertions hold against a fully loaded database.
const fixturePrefix = "test:e3s1:"

// Degrees of latitude per kilometre, near enough at the equator to place
// fixtures at predictable distances.
const kmPerDegreeLat = 111.32

func testPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("CAREGRAPH_TEST_DSN")
	if dsn == "" {
		t.Skip("CAREGRAPH_TEST_DSN not set")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := pool.Ping(context.Background()); err != nil {
		pool.Close()
		t.Fatalf("ping: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

type fixture struct {
	suffix   string
	name     string
	typ      Type
	latKm    float64 // offset north of the origin, in kilometres
	hasPoint bool
	address  bool
}

func seed(t *testing.T, pool *pgxpool.Pool, fixtures []fixture) {
	t.Helper()
	ctx := context.Background()

	clean := func() {
		if _, err := pool.Exec(ctx,
			`DELETE FROM care_infrastructure WHERE source_id LIKE $1`, fixturePrefix+"%",
		); err != nil {
			t.Fatalf("cleanup: %v", err)
		}
	}
	clean()
	t.Cleanup(clean)

	for _, f := range fixtures {
		var lat, lng any
		if f.hasPoint {
			lat, lng = f.latKm/kmPerDegreeLat, 0.0
		}
		var street, plz, ort any
		if f.address {
			street, plz, ort = "Teststraße 1", "10115", "Testort"
		}

		_, err := pool.Exec(ctx, `
			INSERT INTO care_infrastructure
			    (source_id, type, name, strasse, plz, ort, location, details)
			VALUES ($1, $2::provider_type, $3, $4, $5, $6,
			        CASE WHEN $7::double precision IS NULL THEN NULL
			             ELSE ST_SetSRID(ST_MakePoint($8::double precision,
			                                          $7::double precision), 4326)::geography
			        END,
			        $9::jsonb)`,
			fixturePrefix+f.suffix, string(f.typ), f.name,
			street, plz, ort, lat, lng, `{"seeded": true}`)
		if err != nil {
			t.Fatalf("seed %s: %v", f.suffix, err)
		}
	}
}

// standardFixtures: three inside a 10 km radius, one far outside, one with no
// coordinates at all.
func standardFixtures() []fixture {
	return []fixture{
		{"near", "Fixture Near", TypePflegedienstAmbulant, 0, true, true},
		{"mid", "Fixture Mid", TypePflegeheimStationaer, 2, true, true},
		{"edge", "Fixture Edge", TypePflegedienstAmbulant, 8, true, false},
		{"far", "Fixture Far", TypePflegedienstAmbulant, 60, true, true},
		{"nogeo", "Fixture Without Location", TypePflegedienstAmbulant, 0, false, true},
	}
}

func names(ps []Provider) []string {
	out := make([]string, len(ps))
	for i, p := range ps {
		out[i] = p.Name
	}
	return out
}

func TestNearIntegration(t *testing.T) {
	pool := testPool(t)
	seed(t, pool, standardFixtures())
	repo := NewPostgresRepository(pool)
	ctx := context.Background()

	t.Run("returns only rows inside the radius, nearest first", func(t *testing.T) {
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 10, Limit: 100})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}

		want := []string{"Fixture Near", "Fixture Mid", "Fixture Edge"}
		if diff := names(got); len(diff) != len(want) {
			t.Fatalf("got %v, want %v", diff, want)
		}
		for i, n := range want {
			if got[i].Name != n {
				t.Errorf("position %d = %q, want %q (full: %v)", i, got[i].Name, n, names(got))
			}
		}
	})

	t.Run("distance is reported in kilometres", func(t *testing.T) {
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 10, Limit: 100})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		for i, want := range []float64{0, 2, 8} {
			if got[i].DistanceKm == nil {
				t.Fatalf("%s has no distance", got[i].Name)
			}
			// Tolerance covers the spherical-vs-ellipsoidal difference.
			if math.Abs(*got[i].DistanceKm-want) > 0.1 {
				t.Errorf("%s distance = %.3f km, want ~%.0f km",
					got[i].Name, *got[i].DistanceKm, want)
			}
		}
	})

	t.Run("radius excludes the far row", func(t *testing.T) {
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 100, Limit: 100})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		if len(got) != 4 {
			t.Fatalf("got %v, want 4 rows including Fixture Far", names(got))
		}
		if got[3].Name != "Fixture Far" {
			t.Errorf("last row = %q, want Fixture Far", got[3].Name)
		}
	})

	t.Run("type filter narrows results", func(t *testing.T) {
		typ := TypePflegeheimStationaer
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 10, Type: &typ, Limit: 100})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		if len(got) != 1 || got[0].Name != "Fixture Mid" {
			t.Fatalf("got %v, want only Fixture Mid", names(got))
		}
		if got[0].Type != TypePflegeheimStationaer {
			t.Errorf("type = %q", got[0].Type)
		}
	})

	t.Run("limit truncates the nearest rows", func(t *testing.T) {
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 10, Limit: 2})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		if len(got) != 2 {
			t.Fatalf("got %d rows, want 2", len(got))
		}
		if got[0].Name != "Fixture Near" || got[1].Name != "Fixture Mid" {
			t.Errorf("got %v, want the two nearest", names(got))
		}
	})

	t.Run("rows without coordinates never appear", func(t *testing.T) {
		// A generous radius must still not surface the row with a NULL location.
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 100, Limit: 100})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		for _, p := range got {
			if p.Name == "Fixture Without Location" {
				t.Fatal("row with NULL location was returned")
			}
		}
	})

	t.Run("null address columns scan into empty strings", func(t *testing.T) {
		got, err := repo.Near(ctx, NearParams{Lat: 0, Lng: 0, RadiusKm: 10, Limit: 100})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		var edge *Provider
		for i := range got {
			if got[i].Name == "Fixture Edge" {
				edge = &got[i]
			}
		}
		if edge == nil {
			t.Fatal("Fixture Edge missing")
		}
		if edge.Address.Street != "" || edge.Address.PostalCode != "" || edge.Address.City != "" {
			t.Errorf("address = %+v, want zero values", edge.Address)
		}
		if edge.IKNummer != nil {
			t.Errorf("ik_nummer = %v, want nil", *edge.IKNummer)
		}
		if edge.Details["seeded"] != true {
			t.Errorf("details = %v, want the seeded JSONB", edge.Details)
		}
	})

	t.Run("empty result is an empty slice, not nil", func(t *testing.T) {
		// Antarctic waters: nothing within a kilometre.
		got, err := repo.Near(ctx, NearParams{Lat: -75, Lng: 0, RadiusKm: 1, Limit: 10})
		if err != nil {
			t.Fatalf("Near: %v", err)
		}
		if got == nil {
			t.Error("got nil, want an empty slice")
		}
		if len(got) != 0 {
			t.Errorf("got %v, want no rows", names(got))
		}
	})
}
