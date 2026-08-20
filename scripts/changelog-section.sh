#!/bin/sh
# Print the CHANGELOG entry for one version — everything between its heading and
# whatever ends the section. Empty output means there is no entry, which the
# release workflow treats as an error rather than shipping a blank release.

set -eu
awk -v v="$1" '
  $0 ~ "^## \\[" v "\\]"  { inside = 1; next }
  inside && /^## \[/      { exit }
  inside && /^\[[^]]+\]:/ { exit }   # the link definitions at the foot
  inside                  { print }
' CHANGELOG.md | sed '/./,$!d' | awk 'NF {p = NR} {l[NR] = $0} END {for (i = 1; i <= p; i++) print l[i]}'
