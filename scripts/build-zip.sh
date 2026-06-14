#!/usr/bin/env bash
# build-zip.sh — pack vf-clamp.roboFontExt into a distributable zip with a
# checksum so users can verify the artifact against the source commit.
#
# Reproducibility: deterministic mtimes, deterministic locale, deterministic
# timezone. Mirrors the build-zip.sh in the Glyphs plugin so the two
# extensions ship from the same provenance pattern.

set -euo pipefail

export LANG=C
export LC_ALL=C
export TZ=UTC

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="vf-clamp.roboFontExt"
OUT="vf-clamp-robofont.zip"

cd "$ROOT"

if [ ! -d "$BUNDLE" ]; then
	echo "Error: $BUNDLE not found in $ROOT" >&2
	exit 1
fi

# Refuse to package if version sources disagree.
bash "$ROOT/scripts/check-version-parity.sh"

PLIST_VERSION="$(awk '/<key>version<\/key>/{getline; gsub(/.*<string>|<\/string>.*/, ""); print; exit}' "$BUNDLE/info.plist")"

# Strip developer artifacts.
find "$BUNDLE" -name ".DS_Store" -delete
find "$BUNDLE" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE" -name "*.pyc" -delete
find "$BUNDLE" -name "*.pyo" -delete

rm -f "$OUT" "$OUT.sha256"

# Provenance: every bundled file must be tracked by git.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	UNTRACKED="$(git ls-files --others --exclude-standard "$BUNDLE" 2>/dev/null || true)"
	if [ -n "$UNTRACKED" ]; then
		echo "Error: $BUNDLE contains files not tracked by git:" >&2
		printf '  %s\n' $UNTRACKED >&2
		exit 1
	fi
fi

# Deterministic timestamps.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
	if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct -- "$BUNDLE" 2>/dev/null || echo 0)"
	fi
fi
if [ -n "${SOURCE_DATE_EPOCH:-}" ] && [ "$SOURCE_DATE_EPOCH" != "0" ]; then
	TS="$(TZ=UTC date -r "$SOURCE_DATE_EPOCH" +%Y%m%d%H%M.%S 2>/dev/null || true)"
	if [ -n "$TS" ]; then
		find "$BUNDLE" -exec touch -t "$TS" {} +
	fi
fi

zip -X -r --symlinks "$OUT" "$BUNDLE" \
	-x "*/__pycache__/*" \
	-x "*.pyc" \
	-x "*.pyo" \
	-x "*.DS_Store" \
	-x "*/.git*" \
	>/dev/null

ZIP_BYTES=$(stat -f %z "$OUT" 2>/dev/null || stat -c %s "$OUT")
ZIP_ENTRIES=$(unzip -l "$OUT" | tail -1 | awk '{print $2}')

shasum -a 256 "$OUT" > "$OUT.sha256"

echo "Built $OUT ($ZIP_BYTES bytes, $ZIP_ENTRIES entries, version $PLIST_VERSION)"
cat "$OUT.sha256"

# Per-build snapshot — best-effort. Renders the hull plot + animated specimen
# via tools/render_views.py and saves to versions/views-v$PLIST_VERSION.png.
SNAPSHOT_DIR="$ROOT/versions"
SNAPSHOT_PATH="$SNAPSHOT_DIR/views-v$PLIST_VERSION.png"
mkdir -p "$SNAPSHOT_DIR"
RENDER_PY="$HOME/.pyenv/shims/python3"
if [ ! -x "$RENDER_PY" ]; then
	RENDER_PY="$(command -v python3 || true)"
fi
if [ -n "$RENDER_PY" ] && [ -f "$ROOT/tools/render_views.py" ]; then
	if "$RENDER_PY" -c "import objc, AppKit" >/dev/null 2>&1; then
		"$RENDER_PY" "$ROOT/tools/render_views.py" \
			--out "$SNAPSHOT_PATH" >/dev/null 2>&1 || true
		if [ -f "$SNAPSHOT_PATH" ]; then
			echo "Snapshot: versions/views-v$PLIST_VERSION.png"
		fi
	fi
fi
