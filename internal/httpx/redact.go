package httpx

import (
	"net/url"
	"strings"
	"unicode/utf8"
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
	// output is ASCII and cutting on a byte boundary cannot split a rune. A path
	// can hold real UTF-8, so SafePath cuts on a rune boundary instead.
	maxLoggedBytes = 512

	truncationMark = "…[truncated]"
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
		return encoded[:maxLoggedBytes] + truncationMark
	}
	return encoded
}

// SafePath returns a request path as it may be logged: bounded, with control
// characters percent-encoded.
//
// Unlike a query, a path really can carry them — `/a%0Ab` decodes to a newline
// in URL.Path, while url.Values.Encode hands a query back already escaped. The
// JSON handler escapes both, so this is not what prevents a forged record; it
// keeps the guarantee true for any sink that is not JSON, and it bounds a path
// that a caller may otherwise stretch to the header limit.
//
// Encoded rather than stripped. Deleting the characters maps two different paths
// onto one string, which is precisely how a forged path stops being
// distinguishable from a real one in a search.
// The budget is spent while encoding rather than by cutting first: escaping a
// control byte turns one byte into three, so a path trimmed to the cap and
// encoded afterwards comes back over it. FuzzSafePath found exactly that, at 528
// bytes against a cap of 526.
func SafePath(raw string) string {
	// Almost every real path takes this branch: two cheap scans and no allocation.
	if len(raw) <= maxLoggedBytes &&
		strings.IndexFunc(raw, isControl) < 0 && utf8.ValidString(raw) {
		return raw
	}

	var b strings.Builder
	b.Grow(min(len(raw), maxLoggedBytes) + len(truncationMark))

	// Ranging by rune keeps multi-byte characters whole; invalid bytes arrive as
	// utf8.RuneError, so the result is valid UTF-8 whatever came in.
	for _, r := range raw {
		width := utf8.RuneLen(r)
		if isControl(r) {
			width = 3
		}
		if b.Len()+width > maxLoggedBytes {
			b.WriteString(truncationMark)
			return b.String()
		}
		if isControl(r) {
			b.WriteByte('%')
			b.WriteByte(upperHex[byte(r)>>4])
			b.WriteByte(upperHex[byte(r)&0x0f])
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

// Multi-byte runes are untouched: every byte of one is >= 0x80.
func isControl(r rune) bool { return r < 0x20 || r == 0x7f }

const upperHex = "0123456789ABCDEF"
