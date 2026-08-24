package httpx

import (
	"net/url"
	"strings"
)

// Query parameters whose values may appear in a log — listed, rather than the
// dangerous ones excluded.
//
// This was first written the other way round, naming `lat` and `lng` as the
// sensitive ones. That is the same mistake the dataset exporter already made and
// corrected: with an exclusion list, every parameter added later is logged
// silently and nothing announces it. Here that would mean a future `postal_code`
// filed next to `client_ip` on every request, discovered by whoever reads the
// logs rather than by a test.
//
// Anything absent keeps its name and loses its value, so an unlisted parameter
// reads as REDACTED in the record: visible, harmless, and a deliberate decision
// to add. `q` and `city` are deliberately not here — a search term and a place
// are both a caller's interest, recorded beside their address.
var loggableParams = map[string]struct{}{
	"radius_km": {},
	"limit":     {},
	"type":      {},
}

const (
	redactedValue = "REDACTED"

	// A longer query is not parsed at all. MaxHeaderBytes is unset, so Go allows
	// roughly a megabyte of request line, and this runs on every request: without
	// a bound one caller could make the process allocate that much per request
	// and fill the log disk with it.
	maxQueryBytes = 2048

	// The result is bounded too, because parameter *names* come from the caller
	// as well and survive redaction. Encode percent-encodes non-ASCII, so the
	// output is ASCII and cutting on a byte boundary cannot split a rune.
	maxLoggedBytes = 512
)

// RedactQuery returns the query as it may be logged: every value replaced unless
// its parameter is listed above. A record still shows which query ran and over
// what radius, without recording where or for what.
func RedactQuery(raw string) string {
	if raw == "" {
		return ""
	}
	if len(raw) > maxQueryBytes {
		return "[oversized]"
	}

	values, err := url.ParseQuery(raw)
	if err != nil {
		// Dropped whole rather than logged as it came: whatever made it
		// unparseable is not a reason to trust its contents.
		return "[unparsable]"
	}

	for name, vals := range values {
		// Compared lower-case although the handlers read `lat` exactly. `LAT=52.52`
		// is rejected as a missing parameter, but the coordinate is in the request
		// either way and must not survive into the record.
		if _, loggable := loggableParams[strings.ToLower(name)]; loggable {
			continue
		}
		for i := range vals {
			vals[i] = redactedValue
		}
	}

	encoded := values.Encode()
	if len(encoded) > maxLoggedBytes {
		return encoded[:maxLoggedBytes] + "…[truncated]"
	}
	return encoded
}
