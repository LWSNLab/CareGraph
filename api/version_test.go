package api_test

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

// One version for the release, mirrored into the two artefacts that ship with
// it: the ingestion image and the served contract, which names the build behind
// it. The contract's *breaking* version is the `/v1` in its paths, not a number.
func TestVersionsAgree(t *testing.T) {
	want := readVersion(t, "../VERSION")

	if got := loadSpec(t).Info.Version; got != want {
		t.Errorf("api/openapi.yaml info.version = %q, VERSION says %q — run `make set-version VERSION=%s`",
			got, want, want)
	}

	if got := pyprojectVersion(t); got != want {
		t.Errorf("pipelines/pyproject.toml version = %q, VERSION says %q — run `make set-version VERSION=%s`",
			got, want, want)
	}
}

func TestTheVersionHasAChangelogEntry(t *testing.T) {
	version := readVersion(t, "../VERSION")

	b, err := os.ReadFile("../CHANGELOG.md")
	if err != nil {
		t.Fatalf("read CHANGELOG.md: %v", err)
	}

	heading := "## [" + version + "]"
	if !strings.Contains(string(b), heading) {
		t.Fatalf("CHANGELOG.md has no %q section — the release for %s would have no notes",
			heading, version)
	}

	section := changelogSection(string(b), version)
	if strings.TrimSpace(section) == "" {
		t.Errorf("the %q section is empty", heading)
	}
}

// changelogSection mirrors scripts/changelog-section.sh, which the release
// workflow uses to produce the release body.
func changelogSection(doc, version string) string {
	var out []string
	inside := false
	for _, line := range strings.Split(doc, "\n") {
		switch {
		case strings.HasPrefix(line, "## ["+version+"]"):
			inside = true
		case inside && strings.HasPrefix(line, "## ["):
			return strings.Join(out, "\n")
		case inside && regexp.MustCompile(`^\[[^\]]+\]:`).MatchString(line):
			return strings.Join(out, "\n")
		case inside:
			out = append(out, line)
		}
	}
	return strings.Join(out, "\n")
}

func TestVersionIsSemver(t *testing.T) {
	v := readVersion(t, "../VERSION")
	if !regexp.MustCompile(`^\d+\.\d+\.\d+$`).MatchString(v) {
		t.Errorf("VERSION = %q, want MAJOR.MINOR.PATCH — the release workflow tags v<VERSION>", v)
	}
}

func readVersion(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return strings.TrimSpace(string(b))
}

func pyprojectVersion(t *testing.T) string {
	t.Helper()
	b, err := os.ReadFile("../pipelines/pyproject.toml")
	if err != nil {
		t.Fatalf("read pyproject.toml: %v", err)
	}
	m := regexp.MustCompile(`(?m)^version\s*=\s*"([^"]+)"`).FindSubmatch(b)
	if m == nil {
		t.Fatal("no version in pipelines/pyproject.toml")
	}
	return string(m[1])
}
