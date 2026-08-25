package provider

import (
	"net/url"
	"testing"
)

func query(raw string) url.Values {
	v, err := url.ParseQuery(raw)
	if err != nil {
		panic(err)
	}
	return v
}

func TestParseNearParamsDefaults(t *testing.T) {
	p, err := ParseNearParams(query("lat=52.52&lng=13.405"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.Lat != 52.52 || p.Lng != 13.405 {
		t.Errorf("coordinates = (%v, %v), want (52.52, 13.405)", p.Lat, p.Lng)
	}
	if p.RadiusKm != DefaultRadiusKm {
		t.Errorf("radius = %v, want %v", p.RadiusKm, DefaultRadiusKm)
	}
	if p.Limit != DefaultLimit {
		t.Errorf("limit = %d, want %d", p.Limit, DefaultLimit)
	}
	if p.Type != nil {
		t.Errorf("type = %v, want nil", *p.Type)
	}
}

func TestParseNearParamsAccepts(t *testing.T) {
	tests := []struct {
		name  string
		raw   string
		check func(*testing.T, NearParams)
	}{
		{
			name: "all parameters",
			raw:  "lat=48.7182&lng=10.7781&radius_km=15.5&limit=50&type=pflegeheim_stationaer",
			check: func(t *testing.T, p NearParams) {
				if p.RadiusKm != 15.5 {
					t.Errorf("radius = %v", p.RadiusKm)
				}
				if p.Limit != 50 {
					t.Errorf("limit = %d", p.Limit)
				}
				if p.Type == nil || *p.Type != TypePflegeheimStationaer {
					t.Errorf("type = %v", p.Type)
				}
			},
		},
		{
			// Clients routinely emit `&type=` for an unset filter.
			name: "empty optional values fall back to defaults",
			raw:  "lat=52.52&lng=13.405&type=&radius_km=&limit=",
			check: func(t *testing.T, p NearParams) {
				if p.Type != nil || p.RadiusKm != DefaultRadiusKm || p.Limit != DefaultLimit {
					t.Errorf("got %+v, want defaults", p)
				}
			},
		},
		{
			name: "boundary coordinates",
			raw:  "lat=-90&lng=180",
			check: func(t *testing.T, p NearParams) {
				if p.Lat != -90 || p.Lng != 180 {
					t.Errorf("got (%v, %v)", p.Lat, p.Lng)
				}
			},
		},
		{
			name: "maximum radius and limit",
			raw:  "lat=52.52&lng=13.405&radius_km=100&limit=100",
			check: func(t *testing.T, p NearParams) {
				if p.RadiusKm != MaxRadiusKm || p.Limit != MaxLimit {
					t.Errorf("got radius=%v limit=%d", p.RadiusKm, p.Limit)
				}
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p, err := ParseNearParams(query(tc.raw))
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			tc.check(t, p)
		})
	}
}

func TestParseNearParamsRejects(t *testing.T) {
	tests := []struct {
		name      string
		raw       string
		wantParam string
	}{
		{"missing lat", "lng=13.405", "lat"},
		{"missing lng", "lat=52.52", "lng"},
		{"lat not a number", "lat=abc&lng=13.405", "lat"},
		{"lng not a number", "lat=52.52&lng=xyz", "lng"},
		{"lat above range", "lat=91&lng=13.405", "lat"},
		{"lat below range", "lat=-90.1&lng=13.405", "lat"},
		{"lng above range", "lat=52.52&lng=180.5", "lng"},
		{"lng below range", "lat=52.52&lng=-181", "lng"},

		// ParseFloat accepts these, and every range check against NaN is
		// false — so without an explicit guard they reach PostGIS.
		{"lat NaN", "lat=NaN&lng=13.405", "lat"},
		{"lng Inf", "lat=52.52&lng=Inf", "lng"},
		{"radius NaN", "lat=52.52&lng=13.405&radius_km=NaN", "radius_km"},

		{"radius not a number", "lat=52.52&lng=13.405&radius_km=wide", "radius_km"},
		{"radius zero", "lat=52.52&lng=13.405&radius_km=0", "radius_km"},
		{"radius negative", "lat=52.52&lng=13.405&radius_km=-5", "radius_km"},
		{"radius above maximum", "lat=52.52&lng=13.405&radius_km=100.1", "radius_km"},

		{"limit not an integer", "lat=52.52&lng=13.405&limit=many", "limit"},
		{"limit fractional", "lat=52.52&lng=13.405&limit=10.5", "limit"},
		{"limit zero", "lat=52.52&lng=13.405&limit=0", "limit"},
		{"limit negative", "lat=52.52&lng=13.405&limit=-1", "limit"},
		{"limit above maximum", "lat=52.52&lng=13.405&limit=101", "limit"},

		{"unknown type", "lat=52.52&lng=13.405&type=apotheke", "type"},
		{"type wrong case", "lat=52.52&lng=13.405&type=Krankenkasse", "type"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseNearParams(query(tc.raw))
			if err == nil {
				t.Fatalf("expected an error for %q", tc.raw)
			}
			pe, ok := err.(*ParamError)
			if !ok {
				t.Fatalf("error is %T, want *ParamError", err)
			}
			if pe.Param != tc.wantParam {
				t.Errorf("rejected %q, want %q (%v)", pe.Param, tc.wantParam, err)
			}
		})
	}
}

func TestValidateIK(t *testing.T) {
	valid := []string{"100171007", "000000000", "999999999"}
	for _, ik := range valid {
		if err := ValidateIK(ik); err != nil {
			t.Errorf("ValidateIK(%q) = %v, want nil", ik, err)
		}
	}

	invalid := []string{
		"",
		"12345678",   // too short
		"1234567890", // too long
		"12345678a",  // trailing letter
		"a12345678",  // leading letter
		"123 456789", // space
		"123456789 ", // trailing space
		"+123456789", // sign
		"12345678.9", // decimal point
		"१२३४५६७८९",  // Devanagari digits — \d would accept these, [0-9] must not
	}
	for _, ik := range invalid {
		err := ValidateIK(ik)
		if err == nil {
			t.Errorf("ValidateIK(%q) = nil, want an error", ik)
			continue
		}
		if pe, ok := err.(*ParamError); !ok || pe.Param != "ik_nummer" {
			t.Errorf("ValidateIK(%q) returned %v, want a ParamError on ik_nummer", ik, err)
		}
	}
}

func TestAllTypesAreValid(t *testing.T) {
	for _, tp := range AllTypes() {
		if !tp.Valid() {
			t.Errorf("%q reported invalid", tp)
		}
	}
	if Type("").Valid() {
		t.Error("empty type reported valid")
	}
}
