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
