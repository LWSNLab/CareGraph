// Package api_test enforces that the OpenAPI document describes the service that
// is actually built. A drifted spec is worse than none: a generated client
// compiles, runs, and is wrong.
//
// Covers routes, the error-code and provider-type enums, the real JSON handlers
// emit, and the framework's own failure paths.
//
// External test package because internal/httpapi imports this one; the cycle is
// only legal from _test.
package api_test

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/LWSNLab/caregraph/api"
	"github.com/getkin/kin-openapi/openapi3"
)

// loadSpec parses the embedded document. Parsing lives here rather than in the
// package so that kin-openapi is a test dependency only — the service serves
// the bytes and never interprets them.
func loadSpec(t *testing.T) *openapi3.T {
	t.Helper()
	loader := &openapi3.Loader{IsExternalRefsAllowed: false}
	doc, err := loader.LoadFromData(api.SpecYAML)
	if err != nil {
		t.Fatalf("parse embedded openapi.yaml: %v", err)
	}
	return doc
}

func TestSpecIsAValidOpenAPIDocument(t *testing.T) {
	if err := loadSpec(t).Validate(context.Background()); err != nil {
		t.Fatalf("openapi.yaml is not valid: %v", err)
	}
}

// 3.1 removed `nullable` and changed `exclusiveMinimum` to a number. A 3.1 header
// over 3.0 keywords does not fail loudly — generators drop what they cannot read.
func TestSpecDeclaresThreePointZero(t *testing.T) {
	if got := loadSpec(t).OpenAPI; !strings.HasPrefix(got, "3.0.") {
		t.Errorf("openapi = %q; the document uses 3.0 keywords and must say so", got)
	}
}

// --- routes ---------------------------------------------------------------

// Both directions. This is the check that caught `/healthz` being documented under
// the `/v1` server URL while it is served at the origin.
func TestEveryServedRouteIsDocumented(t *testing.T) {
	served := servedRoutes(t)
	documented := documentedRoutes(t)

	for _, route := range served {
		if !contains(documented, route) {
			t.Errorf("route %s is served but missing from api/openapi.yaml", route)
		}
	}
	for _, route := range documented {
		if !contains(served, route) {
			t.Errorf("route %s is documented but not served by the router", route)
		}
	}
}

// servedRoutes reads the route table off the real router, rewriting Gin's
// `:name` placeholders into OpenAPI's `{name}`.
func servedRoutes(t *testing.T) []string {
	t.Helper()
	out := []string{}
	for _, r := range testRouter(t).Routes() {
		segments := strings.Split(r.Path, "/")
		for i, s := range segments {
			if strings.HasPrefix(s, ":") {
				segments[i] = "{" + s[1:] + "}"
			}
		}
		out = append(out, r.Method+" "+strings.Join(segments, "/"))
	}
	sort.Strings(out)
	return out
}

func documentedRoutes(t *testing.T) []string {
	t.Helper()
	out := []string{}
	for path, item := range loadSpec(t).Paths.Map() {
		for method := range item.Operations() {
			out = append(out, method+" "+path)
		}
	}
	sort.Strings(out)
	return out
}

// --- enums read out of the Go source --------------------------------------

// The codes are parsed out of internal/httpx rather than listed here: a hand-kept
// list would drift exactly like the document it guards, which is how `unavailable`
// reached the API before it reached the spec.
func TestErrorCodesMatchSpec(t *testing.T) {
	declared := constantsOfType(t, "../internal/httpx", "ErrorCode")
	if len(declared) == 0 {
		t.Fatal("found no ErrorCode constants — the source parser is broken, not the spec")
	}

	schema, ok := loadSpec(t).Components.Schemas["Error"]
	if !ok {
		t.Fatal("components.schemas.Error is missing")
	}
	documented := enumStrings(t, schema.Value.Properties["code"])

	assertSameSet(t, "error code", declared, documented)
}

// provider_type is mirrored in three places: the SQL enum, the Go constants and
// this document. The first is covered by the repository's integration tests.
func TestProviderTypesMatchSpec(t *testing.T) {
	declared := constantsOfType(t, "../internal/provider", "Type")
	if len(declared) == 0 {
		t.Fatal("found no provider.Type constants — the source parser is broken")
	}

	schema, ok := loadSpec(t).Components.Schemas["ProviderType"]
	if !ok {
		t.Fatal("components.schemas.ProviderType is missing")
	}

	assertSameSet(t, "provider type", declared, enumStrings(t, schema))
}

// constantsOfType returns the string values of the constants declared with the
// named type in a package directory. Reads the AST because Go keeps no runtime
// record of which constants of a type exist.
func constantsOfType(t *testing.T, dir, typeName string) []string {
	t.Helper()

	fset := token.NewFileSet()
	pkgs, err := parser.ParseDir(fset, dir, func(fi os.FileInfo) bool {
		return !strings.HasSuffix(fi.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", dir, err)
	}

	var values []string
	for _, pkg := range pkgs {
		for _, file := range pkg.Files {
			for _, decl := range file.Decls {
				gen, ok := decl.(*ast.GenDecl)
				if !ok || gen.Tok != token.CONST {
					continue
				}
				// A spec without a type carries the last declared one.
				current := ""
				for _, spec := range gen.Specs {
					vs, ok := spec.(*ast.ValueSpec)
					if !ok {
						continue
					}
					if ident, ok := vs.Type.(*ast.Ident); ok {
						current = ident.Name
					}
					if current != typeName {
						continue
					}
					for _, v := range vs.Values {
						lit, ok := v.(*ast.BasicLit)
						if !ok || lit.Kind != token.STRING {
							continue
						}
						s, err := strconv.Unquote(lit.Value)
						if err != nil {
							t.Fatalf("unquote %s: %v", lit.Value, err)
						}
						values = append(values, s)
					}
				}
			}
		}
	}
	sort.Strings(values)
	return values
}

func enumStrings(t *testing.T, ref *openapi3.SchemaRef) []string {
	t.Helper()
	if ref == nil || ref.Value == nil {
		t.Fatal("schema is missing")
	}
	out := make([]string, 0, len(ref.Value.Enum))
	for _, v := range ref.Value.Enum {
		s, ok := v.(string)
		if !ok {
			t.Fatalf("enum value %v is not a string", v)
		}
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}

func assertSameSet(t *testing.T, label string, inCode, inSpec []string) {
	t.Helper()
	for _, v := range inCode {
		if !contains(inSpec, v) {
			t.Errorf("%s %q exists in Go but is not in api/openapi.yaml", label, v)
		}
	}
	for _, v := range inSpec {
		if !contains(inCode, v) {
			t.Errorf("%s %q is in api/openapi.yaml but no Go constant declares it", label, v)
		}
	}
}

func contains(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}
