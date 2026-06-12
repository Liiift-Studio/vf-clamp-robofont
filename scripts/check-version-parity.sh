#!/usr/bin/env bash
# check-version-parity.sh — refuse to build when info.plist, README.md, and
# CHANGELOG.md disagree on the plugin version. Resolves issue #50 by guarding
# against tag-vs-binary drift the moment any one of the three sources is bumped
# without the others following.
#
# Exit codes:
#   0  all three sources agree
#   1  any disagreement / missing file / unparseable version

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFO_PLIST="$ROOT_DIR/vf-clamp.roboFontExt/info.plist"
README="$ROOT_DIR/README.md"
CHANGELOG="$ROOT_DIR/CHANGELOG.md"

fail() {
	echo "vf-clamp: version-parity check FAILED — $1" >&2
	exit 1
}

[ -f "$INFO_PLIST" ] || fail "info.plist not found at $INFO_PLIST"
[ -f "$README" ]      || fail "README.md not found at $README"
[ -f "$CHANGELOG" ]   || fail "CHANGELOG.md not found at $CHANGELOG"

# info.plist: extract the <string> following the <key>version</key> line.
plist_version="$(awk '/<key>version<\/key>/{getline; gsub(/.*<string>|<\/string>.*/, ""); print; exit}' "$INFO_PLIST")"

# README: first match of "Version: X.Y.Z" (Markdown-bold or plain).
readme_version="$(grep -Eom1 'Version:[[:space:]]*\**[[:space:]]*[0-9]+\.[0-9]+\.[0-9]+' "$README" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' || true)"

# CHANGELOG: first '## X.Y.Z' heading (ignoring '## [Unreleased]').
changelog_version="$(grep -Eom1 '^## [0-9]+\.[0-9]+\.[0-9]+' "$CHANGELOG" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' || true)"

[ -n "$plist_version" ]     || fail "could not parse version from info.plist"
[ -n "$readme_version" ]    || fail "could not parse 'Version: X.Y.Z' from README.md"
[ -n "$changelog_version" ] || fail "could not parse '## X.Y.Z' heading from CHANGELOG.md"

if [ "$plist_version" != "$readme_version" ] || [ "$plist_version" != "$changelog_version" ]; then
	cat >&2 <<EOF
vf-clamp: version-parity check FAILED — sources disagree:
  info.plist :  $plist_version
  README.md  :  $readme_version
  CHANGELOG  :  $changelog_version
Bump every file before building / tagging.
EOF
	exit 1
fi

echo "vf-clamp: version parity OK ($plist_version)"
