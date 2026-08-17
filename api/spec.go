// Package api carries the OpenAPI contract into the binary.
//
// The document is embedded rather than read from disk at startup, so a
// deployment cannot serve a contract that differs from the code it ships with —
// there is no file to forget to copy and none to edit in place on a server.
//
// Nothing here parses it. The OpenAPI machinery that checks the document
// against the handlers lives in the _test files, so kin-openapi and its
// dependencies stay out of the production binary: the service serves these
// bytes, it never interprets them.
package api

import _ "embed"

// SpecYAML is the OpenAPI document, exactly as it is served and as the drift
// tests read it.
//
//go:embed openapi.yaml
var SpecYAML []byte

// SpecContentType is what /openapi.yaml answers with. Registered with IANA in
// RFC 9512; older tooling may still expect text/yaml.
const SpecContentType = "application/yaml"
