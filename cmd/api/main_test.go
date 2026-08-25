package main

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
)

// Log injection is impossible here because the handler encodes JSON, not because
// call sites sanitise their inputs — they do not, and several of them log the
// request path and query straight from the caller.
//
// CodeQL reports every one of those as "Log entries created from user input".
// Dismissing them is only defensible while this holds, so it is asserted rather
// than assumed: switch newLogger to a text handler and this test fails, which is
// the signal that the dismissals no longer apply.
func TestLogValuesCannotForgeARecord(t *testing.T) {
	var out bytes.Buffer
	log := newLogger(&out, slog.LevelInfo)

	forged := "lat=1\n{\"level\":\"ERROR\",\"msg\":\"admin key leaked\"}"
	log.Info("request", "query", forged)

	lines := strings.Split(strings.TrimRight(out.String(), "\n"), "\n")
	if len(lines) != 1 {
		t.Fatalf("one call produced %d records, so a value escaped its field:\n%s",
			len(lines), out.String())
	}

	var record map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &record); err != nil {
		t.Fatalf("the record is not valid JSON: %v\n%s", err, lines[0])
	}
	if record["msg"] != "request" {
		t.Errorf(`msg = %v, want "request" — the injected value took over the record`,
			record["msg"])
	}
	if record["query"] != forged {
		t.Errorf("the value did not survive intact: %v", record["query"])
	}
	if record["service"] != "caregraph-api" {
		t.Errorf(`service = %v, want "caregraph-api"`, record["service"])
	}
}
