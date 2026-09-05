package api_test

import (
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/LWSNLab/caregraph/internal/httpx"
	"github.com/LWSNLab/caregraph/internal/provider"
	"github.com/getkin/kin-openapi/openapi3"
)

// TestSchemasMatchTheGoStructs compares each response schema with the struct that
// produces it, field by field, in both directions.
//
// Validating example responses is not enough: a mutation test showed that giving
// a required field `omitempty` still passed, because every fixture happened to set
// it. The field would vanish only for thin data. Struct tags are what decides, so
// they are what this reads.
func TestSchemasMatchTheGoStructs(t *testing.T) {
	doc := loadSpec(t)

	for _, tc := range []struct {
		schema string
		goType reflect.Type
	}{
		{"CareProvider", reflect.TypeOf(provider.Provider{})},
		{"Address", reflect.TypeOf(provider.Address{})},
		{"InfrastructureListResponse", reflect.TypeOf(provider.ListResponse{})},
		{"Error", reflect.TypeOf(httpx.ErrorBody{})},
	} {
		t.Run(tc.schema, func(t *testing.T) {
			ref, ok := doc.Components.Schemas[tc.schema]
			if !ok {
				t.Fatalf("components.schemas.%s is missing", tc.schema)
			}
			compare(t, tc.schema, ref.Value, tc.goType)
		})
	}
}

func compare(t *testing.T, name string, schema *openapi3.Schema, goType reflect.Type) {
	t.Helper()

	emitted, optional := jsonFields(goType)

	documented := make([]string, 0, len(schema.Properties))
	for prop := range schema.Properties {
		documented = append(documented, prop)
	}
	sort.Strings(documented)

	for _, field := range emitted {
		if !contains(documented, field) {
			t.Errorf("%s: Go emits %q but the schema has no such property", name, field)
		}
	}
	for _, prop := range documented {
		if !contains(emitted, prop) {
			t.Errorf("%s: the schema documents %q but no Go field produces it", name, prop)
		}
	}

	required := append([]string(nil), schema.Required...)
	sort.Strings(required)

	for _, field := range emitted {
		isOptional := contains(optional, field)
		isRequired := contains(required, field)

		switch {
		case isOptional && isRequired:
			t.Errorf("%s: %q is `required` but the Go tag has `omitempty`, "+
				"so it disappears from the response whenever the value is empty",
				name, field)
		case !isOptional && !isRequired:
			t.Errorf("%s: %q is always emitted but is not listed as `required`, "+
				"so clients are told to handle an absence that cannot happen",
				name, field)
		}
	}
}

// jsonFields returns the JSON names a struct emits, and which of them may be
// omitted. Embedded structs are not walked — none of these types use them, and
// silently ignoring one would be worse than not supporting it.
func jsonFields(t reflect.Type) (emitted, optional []string) {
	for i := range t.NumField() {
		field := t.Field(i)
		if field.Anonymous {
			panic("jsonFields does not support embedded structs: " + t.String())
		}
		if !field.IsExported() {
			continue
		}

		tag := field.Tag.Get("json")
		if tag == "-" {
			continue
		}
		parts := strings.Split(tag, ",")
		name := parts[0]
		if name == "" {
			name = field.Name
		}

		emitted = append(emitted, name)
		for _, opt := range parts[1:] {
			if opt == "omitempty" {
				optional = append(optional, name)
			}
		}
	}
	sort.Strings(emitted)
	sort.Strings(optional)
	return emitted, optional
}

// TestTheStorableIdentifierIsNamedAsSuch guards a sentence rather than a shape.
//
// The defect it exists for was exactly that: `id` was described as "Stable
// identifier for this record" while being a per-database primary key, and every
// structural test passed — the field was present, typed and required. A caller
// told a field is stable stores it, and on the next import into a fresh database
// every stored reference silently resolves to nothing.
//
// So this reads the prose. Not for wording, which would be brittle, but for two
// things that cannot both hold by accident: the storable identifier is required,
// and the description of `id` sends the reader to it.
func TestTheStorableIdentifierIsNamedAsSuch(t *testing.T) {
	ref, ok := loadSpec(t).Components.Schemas["CareProvider"]
	if !ok {
		t.Fatal("components.schemas.CareProvider is missing")
	}
	schema := ref.Value

	if !slices.Contains(schema.Required, "source_id") {
		t.Error("source_id is not required; a caller cannot key on a field that may be absent")
	}

	sourceID, ok := schema.Properties["source_id"]
	if !ok {
		t.Fatal("CareProvider has no source_id — there is nothing to store")
	}
	if !strings.Contains(strings.ToLower(sourceID.Value.Description), "store") {
		t.Errorf("source_id does not say it is the one to store:\n%s", sourceID.Value.Description)
	}

	id, ok := schema.Properties["id"]
	if !ok {
		return // Removing it is the other acceptable answer.
	}
	description := strings.ToLower(id.Value.Description)

	// The exact phrase that was wrong, and the shape of the same mistake.
	for _, claim := range []string{"stable identifier", "stable id ", "persistent identifier"} {
		if strings.Contains(description, claim) {
			t.Errorf("`id` is described as a %q, but it is minted per database:\n%s",
				strings.TrimSpace(claim), id.Value.Description)
		}
	}
	if !strings.Contains(description, "source_id") {
		t.Errorf("`id` does not point at the identifier that is storable:\n%s",
			id.Value.Description)
	}
}
