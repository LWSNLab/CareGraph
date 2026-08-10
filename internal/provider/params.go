package provider

import (
	"fmt"
	"math"
	"net/url"
	"strconv"
	"strings"
)

// Bounds of the radius search, mirroring the published contract
// (CareGraph_Doc: api/openapi.yaml → /infrastructure/near).
const (
	DefaultRadiusKm = 10.0
	MaxRadiusKm     = 100.0
	DefaultLimit    = 20
	MaxLimit        = 100
)

// NearParams is a validated radius query. Grouping the arguments keeps callers
// from silently transposing lat and lng, which is the classic geo bug: the
// swap stays syntactically valid and only shows up as wrong results.
type NearParams struct {
	Lat      float64
	Lng      float64
	RadiusKm float64
	Type     *Type
	Limit    int
}

// ParamError is a rejected query parameter. Handlers map it to HTTP 400.
type ParamError struct {
	Param   string
	Message string
}

func (e *ParamError) Error() string {
	return fmt.Sprintf("parameter '%s' %s", e.Param, e.Message)
}

// AllTypes lists the provider types accepted by the API, in the order used in
// error messages. It mirrors the SQL enum provider_type.
func AllTypes() []Type {
	return []Type{
		TypeKrankenkasse,
		TypePflegedienstAmbulant,
		TypePflegeheimStationaer,
		TypePflegestuetzpunkt,
	}
}

// Valid reports whether t is a member of the provider_type enum.
func (t Type) Valid() bool {
	for _, known := range AllTypes() {
		if t == known {
			return true
		}
	}
	return false
}

// ParseNearParams validates a query string into NearParams.
//
// Unparseable optional values are rejected rather than replaced by their
// default: silently turning `radius_km=abc` into 10 km would hand back results
// that look authoritative and answer a different question than the one asked.
func ParseNearParams(q url.Values) (NearParams, error) {
	p := NearParams{RadiusKm: DefaultRadiusKm, Limit: DefaultLimit}

	lat, err := requireFloat(q, "lat")
	if err != nil {
		return p, err
	}
	if lat < -90 || lat > 90 {
		return p, &ParamError{Param: "lat", Message: "must be between -90 and 90"}
	}
	p.Lat = lat

	lng, err := requireFloat(q, "lng")
	if err != nil {
		return p, err
	}
	if lng < -180 || lng > 180 {
		return p, &ParamError{Param: "lng", Message: "must be between -180 and 180"}
	}
	p.Lng = lng

	if v := value(q, "radius_km"); v != "" {
		r, ok := finite(v)
		if !ok {
			return p, &ParamError{Param: "radius_km", Message: "must be a finite number"}
		}
		if r <= 0 || r > MaxRadiusKm {
			return p, &ParamError{
				Param:   "radius_km",
				Message: fmt.Sprintf("must be greater than 0 and at most %g", MaxRadiusKm),
			}
		}
		p.RadiusKm = r
	}

	if v := value(q, "limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return p, &ParamError{Param: "limit", Message: "must be an integer"}
		}
		if n < 1 || n > MaxLimit {
			return p, &ParamError{
				Param:   "limit",
				Message: fmt.Sprintf("must be between 1 and %d", MaxLimit),
			}
		}
		p.Limit = n
	}

	if v := value(q, "type"); v != "" {
		t := Type(v)
		if !t.Valid() {
			names := make([]string, 0, len(AllTypes()))
			for _, known := range AllTypes() {
				names = append(names, string(known))
			}
			return p, &ParamError{
				Param:   "type",
				Message: "must be one of: " + strings.Join(names, ", "),
			}
		}
		p.Type = &t
	}

	return p, nil
}

func requireFloat(q url.Values, key string) (float64, error) {
	v := value(q, key)
	if v == "" {
		return 0, &ParamError{Param: key, Message: "is required"}
	}
	f, ok := finite(v)
	if !ok {
		return 0, &ParamError{Param: key, Message: "must be a finite number"}
	}
	return f, nil
}

// finite parses a float and rejects NaN and ±Inf. ParseFloat accepts "NaN" and
// "Inf" as valid input, and NaN would slip through every range check below —
// all comparisons against NaN are false — and reach PostGIS as a coordinate.
func finite(v string) (float64, bool) {
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
		return 0, false
	}
	return f, true
}

// value returns the first value for key, trimmed. A present-but-empty value is
// treated as absent: clients routinely emit `&type=` for an unset filter, and
// rejecting that would be pedantic without preventing a wrong answer.
func value(q url.Values, key string) string {
	return strings.TrimSpace(q.Get(key))
}
