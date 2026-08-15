package provider

import (
	"context"
	"errors"
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

func TestGetByIKIntegration(t *testing.T) {
	pool := testPool(t)
	repo := NewPostgresRepository(pool)
	ctx := context.Background()

	// An IK outside the official ranges, so it cannot collide with the 91 real
	// ones even when the test runs against the loaded database.
	const testIK = "999999901"

	clean := func() {
		if _, err := pool.Exec(ctx,
			`DELETE FROM care_infrastructure WHERE source_id LIKE $1`, fixturePrefix+"%",
		); err != nil {
			t.Fatalf("cleanup: %v", err)
		}
	}
	clean()
	t.Cleanup(clean)

	_, err := pool.Exec(ctx, `
		INSERT INTO care_infrastructure
		    (source_id, ik_nummer, type, name, parent_organization, website,
		     strasse, plz, ort, bundesland, details)
		VALUES ($1, $2, 'krankenkasse'::provider_type, $3, $4, $5,
		        $6, $7, $8, $9, $10::jsonb)`,
		fixturePrefix+"ik", testIK, "Fixture Krankenkasse", "Fixture Verband",
		"https://example.invalid", "Teststraße 1", "10115", "Testort", "Berlin",
		`{"seeded": true, "is_bundesweit": true}`)
	if err != nil {
		t.Fatalf("seed: %v", err)
	}

	t.Run("returns the entity with every field mapped", func(t *testing.T) {
		p, err := repo.GetByIK(ctx, testIK)
		if err != nil {
			t.Fatalf("GetByIK: %v", err)
		}
		if p == nil {
			t.Fatal("got nil, want the seeded row")
		}
		if p.IKNummer == nil || *p.IKNummer != testIK {
			t.Errorf("ik_nummer = %v, want %q", p.IKNummer, testIK)
		}
		if p.Type != TypeKrankenkasse || p.Name != "Fixture Krankenkasse" {
			t.Errorf("type=%q name=%q", p.Type, p.Name)
		}
		if p.ParentOrganization == nil || *p.ParentOrganization != "Fixture Verband" {
			t.Errorf("parent_organization = %v", p.ParentOrganization)
		}
		if p.Address.PostalCode != "10115" || p.Address.City != "Testort" || p.Address.State != "Berlin" {
			t.Errorf("address = %+v", p.Address)
		}
		if p.Details["seeded"] != true {
			t.Errorf("details = %v", p.Details)
		}
		// No reference point in a direct lookup, so no distance.
		if p.DistanceKm != nil {
			t.Errorf("distance_km = %v, want nil", *p.DistanceKm)
		}
		if p.ID == "" {
			t.Error("id is empty")
		}
	})

	t.Run("unknown IK is (nil, nil), not an error", func(t *testing.T) {
		// pgx.ErrNoRows must be translated, or the handler answers 500 for a
		// simple miss.
		p, err := repo.GetByIK(ctx, "999999902")
		if err != nil {
			t.Fatalf("GetByIK: %v", err)
		}
		if p != nil {
			t.Errorf("got %+v, want nil", p)
		}
	})

	t.Run("real insurers are addressable", func(t *testing.T) {
		var realIK string
		err := pool.QueryRow(ctx,
			`SELECT ik_nummer FROM care_infrastructure
			  WHERE ik_nummer IS NOT NULL AND source_id NOT LIKE $1
			  ORDER BY ik_nummer LIMIT 1`, fixturePrefix+"%").Scan(&realIK)
		if err != nil {
			t.Skipf("no loaded insurer to look up (%v)", err)
		}

		p, err := repo.GetByIK(ctx, realIK)
		if err != nil {
			t.Fatalf("GetByIK(%s): %v", realIK, err)
		}
		if p == nil {
			t.Fatalf("IK %s is in the table but the lookup returned nothing", realIK)
		}
		if err := ValidateIK(realIK); err != nil {
			t.Errorf("stored IK %q fails the endpoint's own validation: %v", realIK, err)
		}
	})
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

	t.Run("query timeout is surfaced, not swallowed", func(t *testing.T) {
		// An already-expired context must come back as DeadlineExceeded so the
		// handler can answer 504 instead of a generic 500.
		expired, cancel := context.WithTimeout(ctx, 0)
		defer cancel()

		_, err := repo.Near(expired, NearParams{Lat: 0, Lng: 0, RadiusKm: 10, Limit: 10})
		if err == nil {
			t.Fatal("expected an error from an expired context")
		}
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("error = %v, want it to wrap context.DeadlineExceeded", err)
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

func TestBySourceIDsPreservesTheGivenOrder(t *testing.T) {
	pool := testPool(t)
	seed(t, pool, standardFixtures())
	repo := NewPostgresRepository(pool)
	ctx := context.Background()

	// Deliberately not the insertion order: `WHERE source_id = ANY(…)` returns
	// rows however the planner likes, which would silently discard the ranking a
	// search engine just computed. `array_position` is what restores it.
	want := []string{
		fixturePrefix + "far",
		fixturePrefix + "near",
		fixturePrefix + "edge",
		fixturePrefix + "mid",
	}

	got, err := repo.BySourceIDs(ctx, want)
	if err != nil {
		t.Fatalf("BySourceIDs: %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("got %d rows, want %d", len(got), len(want))
	}

	expected := []string{"Fixture Far", "Fixture Near", "Fixture Edge", "Fixture Mid"}
	for i, name := range expected {
		if got[i].Name != name {
			t.Fatalf("position %d = %q, want %q (full: %v)", i, got[i].Name, name, names(got))
		}
	}
}

func TestBySourceIDsSkipsIdentifiersWithNoRow(t *testing.T) {
	// The index can briefly hold a row the database no longer has. A stale hit
	// should disappear from the results, not fail the whole request.
	pool := testPool(t)
	seed(t, pool, standardFixtures())
	repo := NewPostgresRepository(pool)

	got, err := repo.BySourceIDs(context.Background(), []string{
		fixturePrefix + "near", "stoid:does-not-exist", fixturePrefix + "mid",
	})
	if err != nil {
		t.Fatalf("BySourceIDs: %v", err)
	}
	if len(got) != 2 || got[0].Name != "Fixture Near" || got[1].Name != "Fixture Mid" {
		t.Errorf("got %v, want the two that exist, in order", names(got))
	}
}

func TestBySourceIDsWithNoIdentifiersIsAnEmptySliceNotNil(t *testing.T) {
	pool := testPool(t)
	got, err := NewPostgresRepository(pool).BySourceIDs(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if got == nil {
		t.Error("got nil, want an empty slice")
	}
}
