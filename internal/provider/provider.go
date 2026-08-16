// Package provider is the care-infrastructure domain: types, the persistence
// port, and HTTP handlers for spatial and lookup queries.
package provider

import "errors"

// ErrNotImplemented marks skeleton methods that still need a real implementation.
var ErrNotImplemented = errors.New("not implemented")

// Type classifies a care-infrastructure entity. Mirrors the SQL enum provider_type.
type Type string

const (
	TypeKrankenkasse         Type = "krankenkasse"
	TypePflegedienstAmbulant Type = "pflegedienst_ambulant"
	TypePflegeheimStationaer Type = "pflegeheim_stationaer"
	TypePflegestuetzpunkt    Type = "pflegestuetzpunkt"
	TypeKrankenhaus          Type = "krankenhaus"
)

// Address is a structured postal address.
type Address struct {
	Street     string `json:"street"`
	PostalCode string `json:"postal_code"`
	City       string `json:"city"`
	State      string `json:"state,omitempty"`
}

// Provider is a single care-infrastructure entity, matching the API's
// CareProvider schema (docs/api/openapi.yaml).
type Provider struct {
	ID                 string         `json:"id"`
	IKNummer           *string        `json:"ik_nummer,omitempty"`
	Type               Type           `json:"type"`
	Name               string         `json:"name"`
	ParentOrganization *string        `json:"parent_organization,omitempty"`
	Website            *string        `json:"website,omitempty"`
	Address            Address        `json:"address"`
	DistanceKm         *float64       `json:"distance_km,omitempty"`
	Details            map[string]any `json:"details,omitempty"`
}

// ListResponse is the body both list endpoints answer with.
//
// A named type rather than a map literal per handler: the shape is part of the
// contract, and api/schema_test.go reads these tags to check it against the
// OpenAPI document. A `gin.H` built in two places cannot be checked at all, and
// the two copies can drift apart from each other as well as from the spec.
type ListResponse struct {
	// Total is the number of matches. For /search it is the engine's count and
	// may exceed len(Data); for /near the two are equal.
	Total int `json:"total"`

	// Data is never nil — an absent array and an empty one mean different
	// things to a client, and only one of them is true.
	Data []Provider `json:"data"`
}
