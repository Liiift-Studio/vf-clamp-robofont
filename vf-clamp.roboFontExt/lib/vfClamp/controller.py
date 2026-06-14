# vf-clamp controller — vanilla UI for generating restricted variable fonts from named instance ranges.
# Supports two source modes (mirrors the Glyphs plugin): a TTF/OTF file on disk, or a currently-open
# RoboFont UFO/designspace pair compiled to TTFont via ufo2ft.compileVariableTTF.

import logging
import os
import re
import traceback
import warnings

import vanilla

# NSModalResponseOK (1) is the modern constant; NSFileHandlingPanelOKButton is the
# legacy alias that may not exist in newer AppKit/macOS SDKs. Import both defensively.
# (Still preferred to vanilla.dialogs.getFile because we want a single panel reused for
# both file and folder selection, and we want fine-grained control over allowed types.)
try:
	from AppKit import NSOpenPanel, NSModalResponseOK as _OK
except ImportError:
	try:
		from AppKit import NSOpenPanel, NSFileHandlingPanelOKButton as _OK
	except ImportError:
		from AppKit import NSOpenPanel
		_OK = 1  # raw integer fallback

# AppKit primitives used for the colored axis chips, right-aligned labels, and
# key-equivalent buttons. Each import is wrapped because some PyObjC builds
# bundled with older RoboFont versions are missing one or two of these symbols;
# we degrade gracefully to plain text instead of crashing the window build.
try:
	from AppKit import (
		NSColor,
		NSAttributedString,
		NSMutableAttributedString,
		NSForegroundColorAttributeName,
		NSFontAttributeName,
		NSFont,
		NSTextAlignmentRight,
	)
	_APPKIT_ATTRIB_AVAILABLE = True
except ImportError:
	_APPKIT_ATTRIB_AVAILABLE = False
	NSColor = None  # type: ignore
	NSAttributedString = None  # type: ignore
	NSMutableAttributedString = None  # type: ignore
	NSForegroundColorAttributeName = None  # type: ignore
	NSFontAttributeName = None  # type: ignore
	NSFont = None  # type: ignore
	NSTextAlignmentRight = 2  # raw integer fallback for NSRightTextAlignment

from fontTools import ttLib
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

from . import formats
from . import open_font_core
# v1.0.0: framework-agnostic NSView modules shared with the Glyphs plugin.
# Both produce raw NSViews mounted via window.contentView().addSubview_;
# they have no GlyphsApp dependency so they drop straight in here.
from . import hull_plot as _hull_plot_mod
from . import preview_view as _preview_view_mod

log = logging.getLogger('vfClamp')


# ---------------------------------------------------------------------------
# Visual palette — small accent colors per axis, mirrors the Glyphs plugin
# and the website's golden-angle hue system. Two variants: dark-mode (lighter,
# less saturated to read against dark translucent panels) and light-mode
# (deeper to retain WCAG contrast against off-white). Picked at render time
# via NSAppearance. Values are sRGB 0..1 floats.
# ---------------------------------------------------------------------------

AXIS_COLORS_DARK = {
	'wght': (0.40, 0.66, 0.96),   # blue
	'wdth': (0.42, 0.78, 0.52),   # green
	'opsz': (0.66, 0.55, 0.92),   # purple
	'slnt': (0.96, 0.66, 0.42),   # orange
	'ital': (0.96, 0.55, 0.76),   # pink
	'GRAD': (0.95, 0.86, 0.45),   # yellow
}
AXIS_COLORS_LIGHT = {
	'wght': (0.10, 0.36, 0.78),
	'wdth': (0.10, 0.50, 0.22),
	'opsz': (0.36, 0.18, 0.72),
	'slnt': (0.78, 0.36, 0.06),
	'ital': (0.74, 0.20, 0.46),
	'GRAD': (0.62, 0.46, 0.04),
}
DEFAULT_AXIS_COLOR_DARK = (0.60, 0.60, 0.60)
DEFAULT_AXIS_COLOR_LIGHT = (0.30, 0.30, 0.30)


def _is_dark_appearance():
	"""Return True when the current effective appearance is a Dark Aqua variant."""
	try:
		from AppKit import NSApp, NSAppearanceNameDarkAqua
		appearance = NSApp().effectiveAppearance() if NSApp() is not None else None
		if appearance is None:
			return True
		match = appearance.bestMatchFromAppearancesWithNames_([NSAppearanceNameDarkAqua])
		return match == NSAppearanceNameDarkAqua
	except (AttributeError, ImportError, RuntimeError):
		return True


def _nscolor_for_axis(tag):
	"""Return an NSColor for the small chip that sits next to ``tag`` in the hull preview."""
	if NSColor is None:
		return None
	if _is_dark_appearance():
		palette = AXIS_COLORS_DARK
		default = DEFAULT_AXIS_COLOR_DARK
	else:
		palette = AXIS_COLORS_LIGHT
		default = DEFAULT_AXIS_COLOR_LIGHT
	rgb = palette.get(tag, default)
	return NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], 1.0)


# ---------------------------------------------------------------------------
# Version check (fontTools >= 4.13 required for robust instancer range support)
# ---------------------------------------------------------------------------

# Minimum fontTools version known to support range tuples in instantiateVariableFont.
MIN_FONTTOOLS_VERSION = (4, 13, 0)

_SEMVER_NUMERIC_RE = re.compile(r'^(\d+)')


# Partial typing pass (issue #49): the helpers introduced in this round carry
# annotations so callers can be checked under `mypy --strict --follow-imports=skip`.
# The full sweep across legacy callbacks remains deferred.

from typing import Optional, Tuple


def _parse_semver(ver_string: Optional[str]) -> Optional[Tuple[int, int, int]]:
	"""Parse a (potentially loose) version string into a (major, minor, patch) tuple.

	Handles PEP 440-ish strings like '4.13.0', '4.13.0.dev1', '4.13.0+local',
	'4.13.0rc1' by stripping non-numeric suffixes from each dotted component.
	Missing components default to 0. Returns None when the string cannot be
	parsed at all — callers treat that as "unknown" rather than "out of date".
	"""
	if not ver_string:
		return None
	components = []
	for raw in ver_string.split('.')[:3]:
		match = _SEMVER_NUMERIC_RE.match(raw)
		if not match:
			break
		components.append(int(match.group(1)))
	if not components:
		return None
	while len(components) < 3:
		components.append(0)
	return tuple(components)


def _check_fonttools_version():
	"""Warn at import time if RoboFont bundles an older fontTools than we support.

	Catches only the specific failure modes we expect (ImportError on missing
	fontTools, ValueError/AttributeError on degenerate version strings) so a
	genuine programmer error elsewhere isn't silently swallowed (resolves #63).
	"""
	try:
		from fontTools import __version__ as ft_ver
	except ImportError as exc:
		warnings.warn(
			f'vf-clamp: fontTools is not importable ({exc}); cannot verify version.',
			RuntimeWarning,
		)
		return
	parsed = _parse_semver(ft_ver)
	if parsed is None:
		warnings.warn(
			f'vf-clamp: could not parse fontTools version string {ft_ver!r}; skipping version check.',
			RuntimeWarning,
		)
		return
	if parsed < MIN_FONTTOOLS_VERSION:
		warnings.warn(
			f'vf-clamp: fontTools {ft_ver} is older than the minimum tested version '
			f'{".".join(str(p) for p in MIN_FONTTOOLS_VERSION)}; restriction ranges may misbehave.',
			RuntimeWarning,
		)

_check_fonttools_version()


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

# Maximum permitted source font size (bytes) before we refuse to parse it.
# Guards against accidental selection of multi-GB binaries / crafted files.
MAX_FONT_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB

# Name-record platform constants
_PLATFORM_MAC = 1
_PLATFORM_WIN = 3
_LANG_EN_US = 0x0409


def _get_instance_label(name_table, inst, index):
	"""Resolve a stable display label for an fvar instance.

	Prefers subfamilyNameID, falls back to postScriptNameID, then to a synthetic
	'Instance N' label so every instance is selectable even when name records
	are missing or duplicated. Returns (label, suffix) where suffix disambiguates
	collisions.
	"""
	label = name_table.getDebugName(inst.subfamilyNameID)
	if not label:
		ps_id = getattr(inst, 'postscriptNameID', 0xFFFF)
		if ps_id and ps_id != 0xFFFF:
			label = name_table.getDebugName(ps_id)
	if not label:
		label = f'Instance {index + 1}'
	return label


def compute_hull(font, selected_keys):
	"""Compute axis hull (min/max per axis) across selected named instances.

	`selected_keys` is a list of fvar.instances indices (int). Indexing by
	position avoids name-collision and None-label bugs in earlier versions.

	Returns a dict mapping axis tag → pin value (number) when min == max,
	or (min, max) tuple when a range is needed. Axes not touched by the
	selected instances are omitted so instancer leaves them at full range.
	"""
	fvar = font['fvar']
	instances = fvar.instances
	hull = {}
	for key in selected_keys:
		if not (0 <= key < len(instances)):
			continue
		coords = dict(instances[key].coordinates)
		for tag, val in coords.items():
			if tag not in hull:
				hull[tag] = [val, val]
			else:
				hull[tag][0] = min(hull[tag][0], val)
				hull[tag][1] = max(hull[tag][1], val)

	result = {}
	for tag, (lo, hi) in hull.items():
		# When min == max, pin the axis to that value (pass a number, not a tuple).
		# When min != max, restrict to the range using a (lo, hi) tuple.
		# instancer.AxisRange does NOT exist — the correct API is a plain tuple.
		result[tag] = lo if lo == hi else (lo, hi)
	return result


def _sanitize_ps_name(name, max_len=63):
	"""Produce a spec-compliant PostScript name (nameID 6).

	Rules: only [A-Za-z0-9-], no leading hyphen/digit, no consecutive hyphens,
	max 63 chars, never empty. Falls back to 'Untitled' on degenerate input.
	"""
	safe = re.sub(r'[^A-Za-z0-9-]', '', name.replace(' ', '-'))
	# Collapse runs of hyphens.
	safe = re.sub(r'-{2,}', '-', safe)
	# Strip leading non-letter characters (digits, hyphens).
	safe = re.sub(r'^[^A-Za-z]+', '', safe)
	# Strip trailing hyphens.
	safe = safe.rstrip('-')
	if not safe:
		safe = 'Untitled'
	return safe[:max_len]


def _sanitize_vf_prefix(name, max_len=27):
	"""Produce a spec-compliant Variations PS Name Prefix (nameID 25).

	Rules per OT spec: only [A-Za-z0-9] (NO hyphens), ≤27 characters,
	must start with a letter. Falls back to 'Untitled' on degenerate input.
	"""
	safe = re.sub(r'[^A-Za-z0-9]', '', name)
	safe = re.sub(r'^[^A-Za-z]+', '', safe)
	if not safe:
		safe = 'Untitled'
	return safe[:max_len]


def patch_name_table(font, family_name):
	"""Update name table so restricted VF reflects its instance range.

	Updates nameID 1 (Family), 2 (Subfamily), 4 (Full Name), 6 (PostScript Name),
	16/17 (Typographic Family/Subfamily) and 25 (Variations PS Name Prefix when present).
	Also regenerates nameID 3 (Unique Font Identifier) and nameID 5 (Version) is left
	alone; head.fontRevision is bumped separately. Drops non-English localized records
	for these IDs so locale-aware renderers don't surface stale family names.
	"""
	ps_name = _sanitize_ps_name(family_name)
	vf_prefix = _sanitize_vf_prefix(family_name)

	# Subfamily default after clamping to a range: 'Regular' is the canonical
	# value for a multi-style VF that is not specifically bold/italic-only.
	subfamily = 'Regular'
	full_name = f'{family_name} {subfamily}' if subfamily and subfamily != 'Regular' else family_name
	# Unique font identifier: 'Author: Family Regular: YYYY' would need date/author;
	# we use a deterministic but distinctive form so font caches don't collide with
	# the source font.
	unique_id = f'{ps_name};vf-clamp'

	name_table = font['name']
	existing_ids = {r.nameID for r in name_table.names}

	updates = {
		1: family_name,
		2: subfamily,
		3: unique_id,
		4: full_name,
		6: ps_name,
	}
	if 16 in existing_ids:
		updates[16] = family_name
	# OpenType: when nameID 16 is present, nameID 17 must be present too.
	if 16 in existing_ids or 17 in existing_ids:
		updates[17] = subfamily
	if 25 in existing_ids:
		updates[25] = vf_prefix

	# Drop ALL existing records (any language, any platform) for the IDs we are
	# rewriting — prevents stale localized Japanese/German records from leaking
	# the original family name through CSS / OS font matching.
	name_table.names = [
		r for r in name_table.names
		if r.nameID not in updates
	]

	# Re-add canonical English Windows (platformID 3, encodingID 1, langID 0x0409)
	# records. Mac platform records are intentionally omitted: modern OS font
	# matching uses platformID 3 exclusively, and mac_roman cannot encode
	# non-ASCII text without lossy substitution.
	for name_id, value in updates.items():
		name_table.setName(value, name_id, _PLATFORM_WIN, 1, _LANG_EN_US)


def compact_name(first, last):
	"""Strip shared word prefix/suffix — 'Inter Light' + 'Inter Bold' → 'Inter Light-Bold'.

	# Cross-plugin canonical source-of-truth (resolves #64):
	#
	# The TypeScript implementation in
	#   @liiift-studio/vf-clamp  src/core/utils.ts  compactName()
	# is the authoritative reference. Any behavioural change MUST land there
	# first, then be ported here and to the other in-app plugin copies:
	#   - plugins/glyphs/vf-clamp.glyphsPlugin/plugin.py        (compact_name)
	#   - plugins/robofont/vf-clamp.roboFontExt/.../controller.py  (this file)
	#   - plugins/vscode/src/panel.ts webview                  (compactName)
	#
	# Each Python plugin keeps its own copy because vf-clamp's in-app plugins
	# MUST run with zero external Python dependencies (no shared package can
	# be imported across both Glyphs and RoboFont). Document changes in the
	# CHANGELOG under "Synced from vf-clamp" so reviewers can verify drift.
	"""
	if first == last:
		return first
	fw = first.split()
	lw = last.split()
	prefix_len = 0
	while prefix_len < len(fw) and prefix_len < len(lw) and fw[prefix_len] == lw[prefix_len]:
		prefix_len += 1
	suffix_len = 0
	while (suffix_len < len(fw) - prefix_len and
	       suffix_len < len(lw) - prefix_len and
	       fw[-1 - suffix_len] == lw[-1 - suffix_len]):
		suffix_len += 1
	prefix = ' '.join(fw[:prefix_len])
	a = ' '.join(fw[prefix_len:len(fw) - suffix_len if suffix_len else None])
	b = ' '.join(lw[prefix_len:len(lw) - suffix_len if suffix_len else None])
	suffix = ' '.join(fw[len(fw) - suffix_len:]) if suffix_len else ''
	middle = f'{a}-{b}' if a and b else (a or b)
	return ' '.join(filter(None, [prefix, middle, suffix]))


def sanitize_filename(name):
	"""Replace filesystem-unsafe characters with hyphens and strip leading dots/hyphens.

	Also strips parent-directory segments (`..`) and leading dots to prevent
	hidden-file creation. Always returns a non-empty basename.
	"""
	safe = re.sub(r'[/\\:*?"<>|]', '-', name)
	# Strip leading dots so we never create a hidden file.
	safe = safe.lstrip('.')
	# Replace `..` segments to prevent traversal in any context that splits on os.sep.
	safe = safe.replace('..', '_')
	safe = safe.strip('-').strip()
	return safe or 'output'


def _prune_fvar_instances(font, hull):
	"""Remove fvar.instances whose coordinates fall outside the restricted hull.

	fontTools' instancer only filters fully-pinned axes; range-restricted axes
	leave outside-of-range instances intact, so a Light-Bold clamp still
	advertises Thin/Black named instances. This walks new fvar.instances and
	drops any whose coordinate on a restricted axis is outside (lo, hi).
	"""
	if 'fvar' not in font:
		return
	fvar = font['fvar']
	kept = []
	for inst in fvar.instances:
		ok = True
		for tag, val in inst.coordinates.items():
			constraint = hull.get(tag)
			if isinstance(constraint, tuple):
				lo, hi = constraint
				if not (lo <= val <= hi):
					ok = False
					break
			elif constraint is not None:
				# Pinned axis — fontTools should have removed it from fvar already,
				# but if it remains, require exact match.
				if val != constraint:
					ok = False
					break
		if ok:
			kept.append(inst)
	fvar.instances = kept


def _clamp_axis_defaults(font, hull):
	"""Ensure fvar.axes[i].defaultValue lies within the (possibly restricted) range.

	Returns a list of (tag, original, clamped) tuples describing any changes
	so callers can surface a UI warning.
	"""
	clamps = []
	if 'fvar' not in font:
		return clamps
	fvar = font['fvar']
	for ax in fvar.axes:
		constraint = hull.get(ax.axisTag)
		if isinstance(constraint, tuple):
			lo, hi = constraint
			if not (lo <= ax.defaultValue <= hi):
				new_default = max(lo, min(hi, ax.defaultValue))
				clamps.append((ax.axisTag, ax.defaultValue, new_default))
				ax.defaultValue = new_default
	return clamps


def _prune_stat_table(font, hull):
	"""Remove STAT AxisValues that reference axis values outside the restricted hull.

	STAT (Style Attributes) drives style-matching in OS font menus and CSS.
	After axis restriction we must drop AxisValues for styles no longer
	reachable, otherwise the clamped VF still advertises Thin/Black etc.

	Conservative behaviour: only filter when fontTools exposes a STAT table
	with the standard structure. Returns the count of removed AxisValues for
	logging.

	Format-specific handling (resolves #51):

	* Format 1 / 3: drop when Value is outside the hull. Format 3 carries a
	  LinkedValue — when that LinkedValue references an AxisValue we are about
	  to drop (or itself sits outside the hull), the LinkedValue link is
	  cleared so the remaining record doesn't point at a phantom style.
	* Format 2: clamp Range{Min,Max}Value to the hull edges instead of
	  dropping the record outright. The previous conservative "nominal in
	  range" check dropped Format 2 records whose nominal sat inside the hull
	  but whose advertised min/max extended outside it, leaving font menus
	  with no descriptor for the restricted range.
	* Format 4: unchanged — drop if any axis record falls outside the hull.

	After pruning, ElidedFallbackNameID is cleared when it pointed at a name
	record now removed by patch_name_table, so font-matching falls back to
	subfamily rather than a stale style name.
	"""
	removed = 0
	if 'STAT' not in font:
		return removed
	stat = font['STAT'].table
	axis_value_array = getattr(stat, 'AxisValueArray', None)
	design_axis_record = getattr(stat, 'DesignAxisRecord', None)
	if axis_value_array is None or design_axis_record is None:
		return removed
	# Build map: design-axis-index → axis tag.
	axes = list(design_axis_record.Axis)
	index_to_tag = {i: ax.AxisTag for i, ax in enumerate(axes)}

	def _value_in_hull(av):
		"""Return True if this AxisValue refers to an in-hull axis value."""
		fmt = getattr(av, 'Format', None)
		if fmt in (1, 3):
			tag = index_to_tag.get(av.AxisIndex)
			constraint = hull.get(tag)
			if isinstance(constraint, tuple):
				lo, hi = constraint
				return lo <= av.Value <= hi
			return True
		if fmt == 2:
			tag = index_to_tag.get(av.AxisIndex)
			constraint = hull.get(tag)
			if isinstance(constraint, tuple):
				lo, hi = constraint
				# Keep when the nominal sits inside the hull. Range edges are
				# clamped below — see _clamp_format2_edges.
				return lo <= av.NominalValue <= hi
			return True
		if fmt == 4:
			for record in av.AxisValueRecord:
				tag = index_to_tag.get(record.AxisIndex)
				constraint = hull.get(tag)
				if isinstance(constraint, tuple):
					lo, hi = constraint
					if not (lo <= record.Value <= hi):
						return False
			return True
		return True

	axis_values = list(getattr(axis_value_array, 'AxisValue', []))
	kept = [av for av in axis_values if _value_in_hull(av)]
	removed = len(axis_values) - len(kept)
	kept_ids = {id(av) for av in kept}

	# Clamp Format 2 RangeMin/RangeMax to the hull edges so the remaining
	# range descriptor advertises a range that actually exists in the file.
	for av in kept:
		if getattr(av, 'Format', None) != 2:
			continue
		tag = index_to_tag.get(av.AxisIndex)
		constraint = hull.get(tag)
		if not isinstance(constraint, tuple):
			continue
		lo, hi = constraint
		rmin = getattr(av, 'RangeMinValue', None)
		rmax = getattr(av, 'RangeMaxValue', None)
		if rmin is not None and rmin < lo:
			av.RangeMinValue = lo
		if rmax is not None and rmax > hi:
			av.RangeMaxValue = hi

	# Clear Format 3 LinkedValue when the link points outside the hull. We
	# can't always resolve the target by identity (it's a raw float, not a
	# reference), so the conservative rule is "if the LinkedValue itself
	# falls outside the constrained axis, drop the link".
	for av in kept:
		if getattr(av, 'Format', None) != 3:
			continue
		tag = index_to_tag.get(av.AxisIndex)
		constraint = hull.get(tag)
		if not isinstance(constraint, tuple):
			continue
		lo, hi = constraint
		linked = getattr(av, 'LinkedValue', None)
		if linked is None:
			continue
		if not (lo <= linked <= hi):
			# 0 is the spec-prescribed "no link" sentinel. We also flip the
			# format bit so renderers don't expect a usable link.
			av.LinkedValue = 0
			av.Flags = getattr(av, 'Flags', 0) & ~0x0004  # 0x0004 = LinkedValue flag

	axis_value_array.AxisValue = kept

	# ElidedFallbackNameID: the name ID renderers fall back to when no other
	# style matches. If the fallback record matches a record patch_name_table
	# is about to overwrite (nameID 1, 2, 4, 6, 16, 17, 25) the fallback
	# label could leak the old family name; clear to 2 (Regular) so we don't
	# misadvertise the restricted file.
	patched_ids = {1, 2, 4, 6, 16, 17, 25}
	fallback_id = getattr(stat, 'ElidedFallbackNameID', None)
	if fallback_id is not None and fallback_id in patched_ids:
		stat.ElidedFallbackNameID = 2  # Subfamily fallback.

	# Discard the unused kept_ids tracker — kept only as a defensive marker
	# in case future format handlers want to detect cross-references.
	del kept_ids
	return removed


def _recompute_os2_and_macstyle(font):
	"""Update OS/2.usWeightClass, OS/2.fsSelection, head.macStyle to match new wght default.

	Ports the canonical TypeScript implementation in
	@liiift-studio/vf-clamp src/core/clamp.ts:285-338 (getOs2Updater). Without
	this, OS-level font matching still reports the source font's original
	weight metadata even after the design space has been restricted (resolves
	#52). No-op when there is no fvar or no wght axis.
	"""
	if 'fvar' not in font:
		return
	wght_axis = next((ax for ax in font['fvar'].axes if ax.axisTag == 'wght'), None)
	if wght_axis is None:
		return

	new_default = wght_axis.defaultValue
	# OS/2.usWeightClass valid range is 1..1000.
	weight_class = int(round(max(1, min(1000, new_default))))

	if 'OS/2' in font:
		os2 = font['OS/2']
		os2.usWeightClass = weight_class
		# fsSelection bits: 0x20 = BOLD, 0x40 = REGULAR.
		# Mirror the canonical convention: REGULAR when usWeightClass < 600,
		# BOLD when >= 700; neither bit set in the 600-699 mid-weight band so
		# semibold-only ranges don't lie about being either flavour.
		fs = os2.fsSelection
		fs &= ~(0x20 | 0x40)
		if weight_class >= 700:
			fs |= 0x20  # BOLD
		elif weight_class < 600:
			fs |= 0x40  # REGULAR
		os2.fsSelection = fs

	if 'head' in font:
		head = font['head']
		# macStyle bit 0 = bold.
		ms = head.macStyle
		if weight_class >= 700:
			ms |= 0x01
		else:
			ms &= ~0x01
		head.macStyle = ms


def _strip_dsig(font):
	"""Remove the DSIG table — any modification invalidates a digital signature.

	Returns True if a DSIG was removed.
	"""
	if 'DSIG' in font:
		del font['DSIG']
		return True
	return False


def _bump_font_revision(font, baseline=None):
	"""Bump head.fontRevision by 0.001 so font caches differentiate the derivative
	from its source. No-op if the head table is missing.

	When ``baseline`` is provided it represents the original (pre-mutation)
	revision recorded before any in-process mutations. We always bump *from
	that baseline* so repeated generations on the same loaded TTFont keep
	monotonically increasing rather than stacking 0.001 on top of the
	previously-bumped value (resolves #61). When the caller doesn't track a
	baseline the function behaves as before (single +0.001 step).
	"""
	if 'head' not in font:
		return
	head = font['head']
	current = head.fontRevision or 0.0
	if baseline is None:
		baseline = current
	# Step monotonically past the larger of (baseline, current) so a second
	# Generate click on the same cached TTFont moves forward by at least 0.001.
	step_from = max(baseline, current)
	head.fontRevision = round(step_from + 0.001, 3)


# Source extensions we know how to inherit verbatim. WOFF/WOFF2 inputs preserve
# their compressed flavor on the output so a WOFF source never silently
# downgrades to .ttf (resolves #62). Anything outside this set still falls back
# to .ttf because ufo2ft.compileVariableTTF only emits TTF, and unknown
# extensions are almost certainly a misnamed file.
_INHERITABLE_SOURCE_EXTS = ('.ttf', '.otf', '.woff', '.woff2')


def _resolve_output_extension(ext_override, source_path):
	"""Return the output extension. When ext_override is '' inherit from source.

	`source_path` may be None in open-font mode — falls back to .ttf in that case
	because ufo2ft.compileVariableTTF always produces a TTF binary.

	Preserves .woff / .woff2 source extensions so a WOFF input round-trips as
	WOFF on "TTF/OTF (original)" rather than silently dropping back to .ttf.
	"""
	if ext_override:
		return ext_override
	if not source_path:
		return '.ttf'
	src_ext = os.path.splitext(source_path)[1].lower()
	if src_ext in _INHERITABLE_SOURCE_EXTS:
		return src_ext
	return '.ttf'


def produce_restricted_vf(font, selected_keys, family_name, output_path, flavor=None, overwrite=False, revision_baseline=None):
	"""Produce one restricted VF file from an already-loaded TTFont.

	The caller is responsible for opening the TTFont (and closing it) so the
	same font object can be reused for instance listing and generation. Pass
	`flavor='woff'` or `'woff2'` to emit a web-font; pass `None` to keep the
	SFNT format inherited from the source.

	Returns a dict with diagnostic info:
		{ 'clamped_defaults': [(tag, original, clamped), ...],
		  'pruned_instances': int,
		  'pruned_stat_axis_values': int,
		  'stripped_dsig': bool }

	Raises FileExistsError if the target exists and `overwrite=False`.
	"""
	hull = compute_hull(font, selected_keys)
	if not hull:
		raise ValueError('No valid instances selected')

	# Pre-emptive overwrite check.
	if os.path.exists(output_path) and not overwrite:
		raise FileExistsError(output_path)

	# Ensure the output directory exists before writing.
	output_dir = os.path.dirname(output_path)
	if output_dir:
		os.makedirs(output_dir, exist_ok=True)

	# Run the instancer, then post-process the resulting font to fix things
	# fontTools doesn't touch: stale fvar instances, out-of-range axis defaults,
	# STAT AxisValues, DSIG, and font revision.
	partial = instancer.instantiateVariableFont(font, hull)
	original_count = len(partial['fvar'].instances) if 'fvar' in partial else 0
	_prune_fvar_instances(partial, hull)
	pruned_instances = original_count - (len(partial['fvar'].instances) if 'fvar' in partial else 0)
	clamped_defaults = _clamp_axis_defaults(partial, hull)
	pruned_stat = _prune_stat_table(partial, hull)
	stripped_dsig = _strip_dsig(partial)
	# Pass revision_baseline so repeated generations from the same in-memory
	# TTFont still produce monotonically-increasing head.fontRevision values.
	_bump_font_revision(partial, baseline=revision_baseline)
	_recompute_os2_and_macstyle(partial)
	patch_name_table(partial, family_name)

	# Apply web-font compression *after* all table modifications so the WOFF
	# header reflects the final byte stream.
	if flavor:
		partial.flavor = flavor

	partial.save(output_path)

	return {
		'clamped_defaults': clamped_defaults,
		'pruned_instances': pruned_instances,
		'pruned_stat_axis_values': pruned_stat,
		'stripped_dsig': stripped_dsig,
	}


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

# Module-level singleton so a second menu invocation focuses the existing window
# instead of opening a duplicate.
_controller_instance = None


def _fonttools_cache_token():
	"""Return a token identifying the currently-loaded fontTools build.

	Used to detect a fontTools upgrade between two Generate clicks. If the
	token changes, the cached TTFont is invalidated and re-loaded so we never
	mix old-build instancer behaviour with new-build name-patching against the
	same in-memory font (resolves #58). Combines version string + import id so
	a module reload also invalidates.
	"""
	try:
		import fontTools
		return (getattr(fontTools, '__version__', '?'), id(fontTools))
	except ImportError:
		return ('missing', 0)


def open_controller():
	"""Open the vf-clamp window, or focus the existing one if already open.

	This is the entry point called from __init__.py. Keeping it as a function
	(rather than instantiating at import time) prevents stray windows when
	RoboFont scans the bundle or when another module imports `vfClamp`.
	"""
	global _controller_instance
	if _controller_instance is not None and getattr(_controller_instance, 'w', None) is not None:
		try:
			_controller_instance.w.show()
			return _controller_instance
		except Exception:
			# Window was closed — fall through and create a fresh one.
			_controller_instance = None
	_controller_instance = VFClampController()
	return _controller_instance


class VFClampController:
	"""Floating window controller for generating restricted variable fonts.

	Two source modes are supported (mirrors the Glyphs plugin shipped under
	plugins/glyphs/): SOURCE_FILE reads a TTF/OTF binary from disk and runs
	fontTools.varLib.instancer; SOURCE_OPEN_FONT picks a UFO currently open in
	RoboFont, locates its sibling .designspace, compiles a variable TTF via
	ufo2ft.compileVariableTTF, and feeds that through the same instancer
	pipeline. The dialog shows one input row at a time depending on mode.
	"""

	# Source-mode sentinels — must match the strings the Glyphs plugin uses so
	# documentation, tests, and bug-report templates stay consistent across the
	# suite.
	SOURCE_FILE = 'file'
	SOURCE_OPEN_FONT = 'open_font'

	# Format menus. File-mode keeps the four binary outputs that fontTools can
	# emit directly. Open-font mode adds a UFO sentinel that we explicitly
	# refuse — a UFO is a single master, not a VF; offering it on the popup but
	# erroring at Generate time is honest about the domain mismatch and stops
	# users hunting for an option that does not exist.
	BINARY_FORMATS = ('TTF/OTF (original)', 'TTF', 'OTF', 'WOFF', 'WOFF2')
	UFO_FORMAT_LABEL = 'UFO'
	OPEN_FONT_FORMATS = ('TTF/OTF (original)', 'TTF', 'OTF', 'WOFF', 'WOFF2', UFO_FORMAT_LABEL)

	# Sentinel item shown when there are zero open fonts in RoboFont.
	_OPEN_FONT_POPUP_EMPTY = '(no open fonts)'

	# Window dimensions — v1.0.0 grew from 480 to 640 to accommodate the
	# new design-space chart + animated specimen (replaces the v0.x chips
	# fallback). 640 is still well below the 720-px lower bound on most
	# RoboFont users' monitors and keeps the FloatingWindow comfortable.
	WINDOW_WIDTH = 560
	WINDOW_HEIGHT = 640
	MAX_WIDTH = 1200
	MAX_HEIGHT = 1200

	# Layout constants (mirror the Glyphs plugin so the two dialogs look like
	# they came out of the same toolkit).
	PAD = 16
	LABEL_COL_W = 110
	LABEL_GAP = 12
	CONTROL_X = PAD + LABEL_COL_W + LABEL_GAP  # 138
	ROW = 28
	LABEL_H = 20
	FIELD_H = 22
	BTN_H = 24
	# v1.0.0: HULL_H bumped from 64 → 224 to fit the new design-space plot
	# (140) + size-estimate strip (16) + animated specimen (60) + gaps.
	HULL_H = 224
	# Internal split of HULL_H so the build code can place each child
	# without re-deriving the math on every render pass.
	PLOT_H = 140
	SPECIMEN_H = 60

	def __init__(self):
		"""Initialise the controller window and all UI elements."""
		# File-mode state
		self._font_path = None
		self._font = None  # cached TTFont (file-mode)
		# Original head.fontRevision recorded on load so repeated Generate
		# clicks against the same cached TTFont produce monotonically
		# increasing revisions (resolves #61). None until a font is loaded.
		self._font_revision_baseline = None
		# Cache-invalidation token — bumped any time fontTools is reloaded
		# (e.g. RoboFont package update inside the same session). Generates
		# compare against this token and discard the cached TTFont on
		# mismatch so two Generate clicks across a fontTools upgrade can't
		# write subtly different outputs from the "same" loaded font (resolves
		# #58).
		self._font_cache_token = _fonttools_cache_token()

		# Open-font-mode state. `_open_font` is the defcon.Font reference; the
		# designspace path is resolved lazily by `open_font_core.find_sibling_designspace`;
		# `_cached_ttfont_from_designspace` is populated on Generate by
		# `open_font_core.compile_designspace_to_ttfont` and closed after writing.
		self._source_mode = self.SOURCE_FILE
		self._open_font = None
		self._open_font_options = []
		self._designspace_path = None
		self._cached_ttfont_from_designspace = None

		# Shared state
		self._instance_names = []
		self._output_folder = None
		# Dirty flag: True once the user has typed a custom family name, so
		# selection changes no longer overwrite it.
		self._name_dirty = False
		# Re-entrancy guard so a second click on Generate during a synchronous
		# run is a no-op.
		self._generating = False
		# Track the most recently written output so Reveal in Finder works.
		self._last_output_path = None

		self._build_window()
		# Populate the open-font popup before flipping into a default mode so
		# the popup items match the actual state of RoboFont when the dialog
		# opens.
		self._refresh_open_font_popup()
		# Default to Open Font when at least one UFO is open in RoboFont; this
		# mirrors the Glyphs plugin's canonical "I have my font open, clamp it"
		# flow. Otherwise stay in File mode.
		if self._open_font_options:
			self._transition_source_mode(self.SOURCE_OPEN_FONT, clear_inactive=False)
			self._auto_select_frontmost_open_font()
		else:
			self._transition_source_mode(self.SOURCE_FILE, clear_inactive=False)

	# -------------------------------------------------------------------------
	# Window construction
	# -------------------------------------------------------------------------

	def _right_label(self, posSize, text):
		"""Build a right-aligned TextBox label for the left column.

		Ported from the Glyphs plugin so the RoboFont dialog matches the same
		filter-style label column. AppKit alignment lookup is wrapped because
		older PyObjC builds may not expose `cell()` for every NSControl
		subclass; failure is non-fatal.
		"""
		box = vanilla.TextBox(posSize, text)
		try:
			box._nsObject.cell().setAlignment_(NSTextAlignmentRight)
		except Exception:
			pass
		return box

	def _build_window(self):
		"""Build the dialog layout — labels in the left column, controls in the right."""
		w = self.WINDOW_WIDTH
		h = self.WINDOW_HEIGHT
		PAD = self.PAD
		LABEL_COL_W = self.LABEL_COL_W
		CONTROL_X = self.CONTROL_X
		ROW = self.ROW
		LABEL_H = self.LABEL_H
		FIELD_H = self.FIELD_H
		BTN_H = self.BTN_H
		HULL_H = self.HULL_H

		self.w = vanilla.FloatingWindow(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, 420),
			maxSize=(self.MAX_WIDTH, self.MAX_HEIGHT),
			autosaveName='vfClampMainWindow',
		)
		# Wire the close callback so file handles + TTFont memory are released
		# and the module-level singleton is cleared. Without this hook the
		# cached _font keeps the source file mapped and the singleton points at
		# a dead window so the next /vf-clamp invocation tries to .show() a
		# closed FloatingWindow (resolves #59).
		try:
			self.w.bind('close', self._on_window_close)
		except (AttributeError, RuntimeError):
			pass
		win = self.w

		y = PAD

		# --- Row 1: Source selector (RadioGroup) ------------------------------
		win.sourceLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Source:')
		win.sourceRadio = vanilla.RadioGroup(
			(CONTROL_X, y, -PAD, ROW),
			['Open Font', 'File'],
			isVertical=False,
			callback=self._on_source_radio_changed,
		)
		# Default to File visually; __init__ flips it to Open Font after
		# construction if a UFO is open. Setting the radio value *before* the
		# callback fires during _transition_source_mode keeps initial state
		# coherent.
		win.sourceRadio.set(1)
		y += ROW + 6

		# --- Row 2: Source-specific input (file row OR open-font popup) -----
		# Both widget groups share the same Y; only one is visible at a time,
		# controlled by `_set_source_mode_ui`.
		win.sourceInputLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'File:')

		# File-mode widgets
		win.fontPathField = vanilla.EditText(
			(CONTROL_X, y, -110, FIELD_H),
			placeholder='Select a .ttf or .otf variable font…',
			readOnly=True,
		)
		win.browseButton = vanilla.Button(
			(-102, y - 1, 86, BTN_H),
			'Browse…',
			callback=self._on_select_font,
		)

		# Open-font-mode widget (occupies the same Y, full width)
		win.openFontPopup = vanilla.PopUpButton(
			(CONTROL_X, y, -PAD, FIELD_H + 2),
			[self._OPEN_FONT_POPUP_EMPTY],
			callback=self._on_open_font_chosen,
		)
		y += ROW + 8

		# --- Divider ----------------------------------------------------------
		win.divider1 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Row 3: Instances label + bulk-select buttons --------------------
		win.instanceLabel = self._right_label((PAD, y + 2, LABEL_COL_W, LABEL_H), 'Instances:')
		# Bulk-selection buttons mirror the Glyphs plugin's All/None/Invert
		# triad. They sit on the right edge of the row.
		win.allBtn = vanilla.Button(
			(-PAD - 180, y, 56, BTN_H), 'All',
			callback=self._on_select_all, sizeStyle='small',
		)
		win.noneBtn = vanilla.Button(
			(-PAD - 122, y, 56, BTN_H), 'None',
			callback=self._on_select_none, sizeStyle='small',
		)
		win.invertBtn = vanilla.Button(
			(-PAD - 64, y, 64, BTN_H), 'Invert',
			callback=self._on_select_invert, sizeStyle='small',
		)
		y += LABEL_H + 6

		# --- Row 4: Instance list --------------------------------------------
		# vanilla.List is a native NSTableView with allowsMultipleSelection.
		# We keep RoboFont's List rather than the Glyphs CheckBox column —
		# multi-select via Cmd/Shift-click is the canonical Mac idiom and
		# matches the original RoboFont controller's interaction model.
		win.instanceList = vanilla.List(
			(CONTROL_X, y, -PAD, 140),
			[],
			allowsMultipleSelection=True,
			selectionCallback=self._on_selection_change,
		)
		y += 140 + 8

		# --- Row 5: Design space chart + animated specimen (v1.0.0) ---------
		# Replaces the v0.x single-line axis chips with the same hull plot +
		# animated HOHO Anes specimen that ships in the Glyphs plugin. The
		# chips fallback (win.axisPreview) is still constructed below so the
		# refresh code paths that write text into it during error/empty
		# states keep working, but it's hidden when the plot is available.
		win.hullLabel = self._right_label(
			(PAD, y + 2, LABEL_COL_W, LABEL_H), 'Design space:',
		)
		PLOT_H = self.PLOT_H
		SPECIMEN_H = self.SPECIMEN_H
		col_w = w - CONTROL_X - PAD

		# Hull plot — raw NSView, top-left origin. addSubview_ on an
		# unflipped vanilla.FloatingWindow contentView uses bottom-left
		# coords, so we Y-flip first (same pattern as the Glyphs plugin).
		self._hull_plot_view = None
		if _hull_plot_mod.is_available():
			plot_y_flipped = h - y - PLOT_H
			plot_view = _hull_plot_mod.make_hull_plot_view(
				(CONTROL_X, plot_y_flipped, col_w, PLOT_H),
			)
			if plot_view is not None:
				try:
					win._window.contentView().addSubview_(plot_view)
					self._hull_plot_view = plot_view
				except (AttributeError, RuntimeError):
					self._hull_plot_view = None

		# Chips fallback — same TextBox the prior versions used. Hidden
		# when the plot mounted successfully; resurfaced if not (e.g. on
		# a RoboFont build without the AppKit primitives we need).
		win.axisPreview = vanilla.TextBox(
			(CONTROL_X, y, col_w, PLOT_H),
			'(select instances to preview)',
			sizeStyle='small',
			selectable=True,
		)
		try:
			cell = win.axisPreview._nsObject.cell()
			cell.setUsesSingleLineMode_(False)
			cell.setWraps_(True)
		except (AttributeError, RuntimeError):
			pass
		if self._hull_plot_view is not None:
			try:
				win.axisPreview.show(False)
			except (AttributeError, RuntimeError):
				pass

		# Size estimate + structural counter strip sits between plot and
		# specimen. _refresh_size_estimate writes "~38 KB · 5 instances ·
		# 5 masters · 2 ax · 1 pinned" here once a font is loaded.
		win.sizeEstimate = vanilla.TextBox(
			(CONTROL_X, y + PLOT_H + 4, col_w, 16),
			'',
			sizeStyle='small',
			selectable=True,
		)

		# Animated specimen — same NSView the Glyphs plugin uses. Drives
		# the design-space probe ring inside the plot above each tick.
		self._preview_view = None
		if _preview_view_mod.is_available():
			specimen_top = y + PLOT_H + 22
			specimen_y_flipped = h - specimen_top - SPECIMEN_H
			pv = _preview_view_mod.make_preview_view(
				(CONTROL_X, specimen_y_flipped, col_w, SPECIMEN_H),
			)
			if pv is not None:
				try:
					win._window.contentView().addSubview_(pv)
					self._preview_view = pv
					pv.setFontSize_(40.0)
					if self._hull_plot_view is not None:
						pv.setProbeTarget_(self._hull_plot_view)
				except (AttributeError, RuntimeError):
					self._preview_view = None

		y += HULL_H + 8

		# --- Divider ----------------------------------------------------------
		win.divider2 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Row 6: Output Name ----------------------------------------------
		win.outputNameLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Output Name:')
		win.outputNameField = vanilla.EditText(
			(CONTROL_X, y, -PAD, FIELD_H),
			placeholder='Auto-generated from selection…',
			callback=self._on_name_edited,
		)
		y += ROW + 4

		# --- Row 7: Format ---------------------------------------------------
		win.formatLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Format:')
		win.formatPopUp = vanilla.PopUpButton(
			(CONTROL_X, y, 200, FIELD_H + 2),
			list(self.BINARY_FORMATS),
		)
		y += ROW + 4

		# --- Row 8: Output Folder --------------------------------------------
		win.outputFolderLabel = self._right_label((PAD, y + 4, LABEL_COL_W, LABEL_H), 'Folder:')
		win.outputFolderField = vanilla.EditText(
			(CONTROL_X, y, -110, FIELD_H),
			placeholder='Same folder as source…',
			readOnly=True,
		)
		win.chooseFolderButton = vanilla.Button(
			(-102, y - 1, 86, BTN_H),
			'Choose…',
			callback=self._on_choose_folder,
		)
		y += ROW + 8

		# Tooltips + initial disabled-state for controls that depend on a
		# loaded source. Without these, hovering reveals nothing and the
		# disabled "Generate" is the only signal that a font hasn't loaded
		# yet (resolves #57). Tooltips wrapped because older vanilla builds
		# expose setToolTip via _nsObject and not as a top-level method.
		_TOOLTIPS = {
			win.sourceRadio: 'Choose where the variable font comes from.',
			win.fontPathField: 'Path to the .ttf or .otf you want to clamp.',
			win.browseButton: 'Pick a TTF or OTF variable font from disk.',
			win.openFontPopup: 'Pick a UFO that is already open in RoboFont.',
			win.instanceList: 'Cmd-click or Shift-click to multi-select named instances.',
			win.allBtn: 'Select every instance.',
			win.noneBtn: 'Deselect every instance.',
			win.invertBtn: 'Invert the current selection.',
			win.axisPreview: 'Live preview of the licensed design space derived from the selected instances.',
			win.outputNameField: 'Family name written into the output file. Edit to override the auto-generated name.',
			win.formatPopUp: 'Output container format. "TTF/OTF (original)" inherits from the source.',
			win.outputFolderField: 'Where the clamped file will be written.',
			win.chooseFolderButton: 'Pick a different folder for the output file.',
		}
		for control, tip in _TOOLTIPS.items():
			try:
				control._nsObject.setToolTip_(tip)
			except (AttributeError, RuntimeError):
				pass

		# Disable folder + format controls until a source is loaded so the
		# user can't tweak an output for a font that hasn't been opened yet.
		try:
			win.outputFolderField.enable(False)
			win.chooseFolderButton.enable(False)
			win.formatPopUp.enable(False)
		except (AttributeError, RuntimeError):
			pass

		# Accessibility labels — VoiceOver otherwise reads each vanilla
		# EditText as "edit text" with no context. Mirrors the Glyphs plugin's
		# setAccessibilityLabel_ pass for parity (resolves #56).
		_A11Y = {
			win.sourceRadio: 'Variable font source',
			win.fontPathField: 'Source font file path',
			win.browseButton: 'Browse for source font file',
			win.openFontPopup: 'Open font selector',
			win.instanceList: 'Named instances; multi-select to define the clamp range',
			win.allBtn: 'Select all instances',
			win.noneBtn: 'Deselect all instances',
			win.invertBtn: 'Invert instance selection',
			win.axisPreview: 'Design space preview',
			win.outputNameField: 'Output family name',
			win.formatPopUp: 'Output container format',
			win.outputFolderField: 'Output folder path',
			win.chooseFolderButton: 'Choose output folder',
			win.generateButton: 'Generate restricted variable font',
			win.cancelButton: 'Cancel and close window',
			win.revealButton: 'Reveal output file in Finder',
			win.statusLabel: 'Status messages',
		}
		for control, label in _A11Y.items():
			try:
				control._nsObject.setAccessibilityLabel_(label)
			except (AttributeError, RuntimeError):
				pass

		# --- Divider ----------------------------------------------------------
		win.divider3 = vanilla.HorizontalLine((PAD, y, -PAD, 1))
		y += 12

		# --- Bottom action bar -----------------------------------------------
		# Layout: [statusLabel] ... [Reveal] [Cancel] [Generate]
		GEN_W = 110
		CAN_W = 80
		REV_W = 110
		GAP = 8

		win.generateButton = vanilla.Button(
			(-PAD - GEN_W, y, GEN_W, BTN_H),
			'Generate',
			callback=self._on_generate,
		)
		win.generateButton.enable(False)
		# Return key → default action; on macOS this also paints the button
		# with the system accent colour, giving us the "primary blue" look.
		# Setting the key equivalent via the underlying NSButton is the
		# canonical AppKit pattern; vanilla's older `setDefaultButton` does
		# not always paint the button blue on modern macOS.
		try:
			win.generateButton._nsObject.setKeyEquivalent_('\r')
		except Exception:
			pass

		win.cancelButton = vanilla.Button(
			(-PAD - GEN_W - GAP - CAN_W, y, CAN_W, BTN_H),
			'Cancel',
			callback=self._on_cancel,
		)
		# Escape closes the dialog without writing output.
		try:
			win.cancelButton._nsObject.setKeyEquivalent_('\x1b')
		except Exception:
			pass

		win.revealButton = vanilla.Button(
			(-PAD - GEN_W - GAP - CAN_W - GAP - REV_W, y, REV_W, BTN_H),
			'Reveal in Finder',
			callback=self._on_reveal,
			sizeStyle='small',
		)
		win.revealButton.enable(False)
		# Reveal stays hidden until a successful generate creates a file to
		# reveal. Showing a disabled "Reveal" before any output exists is a
		# dead affordance that confuses first-time users.
		try:
			win.revealButton.show(False)
		except Exception:
			pass

		# Status label runs along the left side of the action bar.
		status_right_offset = PAD + GEN_W + GAP + CAN_W + GAP + REV_W + GAP
		win.statusLabel = vanilla.TextBox(
			(PAD, y + 4, -status_right_offset, LABEL_H),
			'',
			sizeStyle='small',
			selectable=True,
		)

		self.w.open()

	# -------------------------------------------------------------------------
	# Internal helpers
	# -------------------------------------------------------------------------

	def _set_status(self, message, error=False):
		"""Set status text. Prefixes 'Error:' when error=True for clarity."""
		text = f'Error: {message}' if error else message
		try:
			self.w.statusLabel.set(text)
		except (AttributeError, RuntimeError):
			pass

	def _update_generate_button(self):
		"""Enable Generate only when a source is loaded and ≥1 instance is selected.

		Also enables/disables the dependent output controls (folder field +
		format popup) in lockstep so they're never tweakable for a font that
		hasn't been opened yet (resolves #57 stale-control surface).
		"""
		indices = self.w.instanceList.getSelection()
		source_loaded = (
			(self._source_mode == self.SOURCE_FILE and self._font_path is not None)
			or (self._source_mode == self.SOURCE_OPEN_FONT and self._open_font is not None)
		)
		enabled = bool(source_loaded and indices) and not self._generating
		self.w.generateButton.enable(enabled)
		try:
			self.w.outputFolderField.enable(source_loaded)
			self.w.chooseFolderButton.enable(source_loaded)
			self.w.formatPopUp.enable(source_loaded)
		except (AttributeError, RuntimeError):
			pass

	def _close_font(self):
		"""Close any cached file-mode TTFont and release the file handle."""
		if self._font is not None:
			try:
				self._font.close()
			except Exception:
				pass
			self._font = None
		# Also forget the recorded baseline revision — a fresh load gets a
		# fresh baseline on the next _load_instances call.
		self._font_revision_baseline = None

	def _on_window_close(self, sender):
		"""Release file handles and clear the module-level singleton on window close.

		Without this, the cached TTFont keeps the source file mapped and the
		singleton in `_controller_instance` points at a dead window — the next
		menu invocation tries to `.show()` a closed FloatingWindow and silently
		fails to open the dialog (resolves #59).
		"""
		global _controller_instance
		self._close_font()
		self._close_designspace_ttfont()
		if _controller_instance is self:
			_controller_instance = None

	def _close_designspace_ttfont(self):
		"""Close any in-memory TTFont compiled from a designspace.

		fontTools' TTFont does not strictly require explicit close, but dropping
		the reference lets the GC reclaim parsed tables immediately when the
		user switches between open fonts.
		"""
		if self._cached_ttfont_from_designspace is not None:
			try:
				self._cached_ttfont_from_designspace.close()
			except Exception:
				pass
			self._cached_ttfont_from_designspace = None

	# -------------------------------------------------------------------------
	# Source-mode plumbing (mirrors Glyphs plugin _set_source_mode_ui etc.)
	# -------------------------------------------------------------------------

	def _set_source_mode_ui(self, mode):
		"""Show the file row OR the open-font popup, depending on ``mode``.

		Both widget groups occupy the same Y position; only one is visible.
		Also updates the leading "Source-input" label and refreshes the
		format popup so the UFO option appears/disappears as appropriate.

		This is the visibility-only leaf. For a full source-mode transition
		(clearing the inactive source's pointer, resetting widgets, refreshing
		the popup contents) call `_transition_source_mode` instead.
		"""
		is_file = (mode == self.SOURCE_FILE)
		try:
			self.w.fontPathField.show(is_file)
			self.w.browseButton.show(is_file)
			self.w.openFontPopup.show(not is_file)
			self.w.sourceInputLabel.set('File:' if is_file else 'Open Font:')
			# Reflect the active mode on the radio. Setting via vanilla bypasses
			# the callback so we don't recurse.
			self.w.sourceRadio.set(1 if is_file else 0)
		except (AttributeError, RuntimeError):
			pass
		self._source_mode = mode
		self._refresh_format_popup()

	def _transition_source_mode(self, mode, *, clear_inactive=True, reset_folder=False):
		"""Switch ``_source_mode`` and clear stale cross-source state.

		Centralised transition (mirrors Glyphs plugin issue #44) so file paths,
		open-font references, the instance list, the hull preview, and the
		default output folder cannot drift out of sync with the visible widget
		set. Callers that are about to populate the inactive source themselves
		(e.g. __init__'s default mode) pass `clear_inactive=False`.
		"""
		# Flip visibility + format popup first so a downstream refresh sees
		# the correct widget set.
		self._set_source_mode_ui(mode)

		if clear_inactive:
			if mode == self.SOURCE_FILE:
				# Switching INTO file mode → drop open-font references.
				self._open_font = None
				self._designspace_path = None
				self._close_designspace_ttfont()
				try:
					self.w.openFontPopup.set(0)
				except (AttributeError, RuntimeError):
					pass
			else:
				# Switching INTO open-font mode → drop the file-path reference
				# and blank the field so the user sees the source has changed.
				self._font_path = None
				self._close_font()
				try:
					self.w.fontPathField.set('')
				except (AttributeError, RuntimeError):
					pass

		if reset_folder:
			try:
				self.w.outputFolderField.set('')
				self._output_folder = None
			except (AttributeError, RuntimeError):
				pass

	def _refresh_open_font_popup(self):
		"""Repopulate the open-font popup from RoboFont's current AllFonts() list."""
		fonts = open_font_core.list_open_fonts() if open_font_core.is_robofont_available() else []
		self._open_font_options = fonts
		if not fonts:
			items = [self._OPEN_FONT_POPUP_EMPTY]
		else:
			# Leading sentinel so opening the popup is the explicit action that
			# switches source — picking the first real entry shouldn't fire on
			# mere refresh.
			items = [self._OPEN_FONT_POPUP_EMPTY] + [open_font_core.open_font_label(f) for f in fonts]
		try:
			self.w.openFontPopup.setItems(items)
			self.w.openFontPopup.set(0)
		except Exception:
			pass

	def _on_open_font_chosen(self, sender):
		"""Handle a user selection from the open-font popup."""
		idx = self.w.openFontPopup.get()
		# Index 0 is the sentinel "(no open fonts)" entry.
		if idx <= 0 or idx - 1 >= len(self._open_font_options):
			return
		font = self._open_font_options[idx - 1]
		self._load_open_font(font)

	def _on_source_radio_changed(self, sender):
		"""User toggled the Source: Open Font / File radio."""
		idx = self.w.sourceRadio.get()
		if idx == 0:  # Open Font
			# Refresh the popup in case the user opened/closed UFOs between
			# dialog launch and now.
			self._refresh_open_font_popup()
			if not self._open_font_options:
				self._set_status(
					'No fonts are open in RoboFont. Switch back to File or open one.'
				)
				# Still transition so the UI matches the radio click. The
				# transition clears any stale file path so the user's next
				# move starts from a clean slate.
				self._transition_source_mode(self.SOURCE_OPEN_FONT)
				return
			self._transition_source_mode(self.SOURCE_OPEN_FONT)
			self._auto_select_frontmost_open_font()
		else:  # File
			self._transition_source_mode(self.SOURCE_FILE)
			self._set_status('')

	def _auto_select_frontmost_open_font(self):
		"""Default to the frontmost open RoboFont font when the dialog opens.

		Uses object identity (`is`) for the frontmost match because RoboFont's
		AllFonts() can reissue RFont wrappers for the same underlying defcon.Font;
		`in` may falsely report "not found" and silently pick the wrong default.
		Falls back to the first listed font when there is no identity match.
		"""
		if not self._open_font_options:
			return
		frontmost = open_font_core.frontmost_open_font()
		target = self._open_font_options[0]
		if frontmost is not None:
			for candidate in self._open_font_options:
				if candidate is frontmost:
					target = candidate
					break
		try:
			# +1 for the sentinel row at popup index 0
			idx = self._open_font_options.index(target) + 1
			self.w.openFontPopup.set(idx)
		except (ValueError, AttributeError, RuntimeError):
			pass
		self._load_open_font(target)

	def _refresh_format_popup(self):
		"""Adjust the Format popup items to match the active source mode.

		File mode → BINARY_FORMATS only. Open-font mode adds the UFO sentinel
		at the end so users see it exists, but Generate refuses to write it
		(UFO is a single master, not a VF — clamping it does not make sense).
		"""
		if self._source_mode == self.SOURCE_OPEN_FONT:
			items = list(self.OPEN_FONT_FORMATS)
			default_idx = 0
		else:
			items = list(self.BINARY_FORMATS)
			default_idx = 0
		# Try to preserve the user's existing selection if still valid.
		try:
			current_idx = self.w.formatPopUp.get()
			current_items = self.w.formatPopUp.getItems()
			current_label = current_items[current_idx] if 0 <= current_idx < len(current_items) else None
		except Exception:
			current_label = None
		try:
			self.w.formatPopUp.setItems(items)
			if current_label in items:
				self.w.formatPopUp.set(items.index(current_label))
			else:
				self.w.formatPopUp.set(default_idx)
		except Exception:
			pass

	# -------------------------------------------------------------------------
	# File-mode callbacks
	# -------------------------------------------------------------------------

	def _on_select_font(self, sender):
		"""Open a file picker, load the font, and populate the instance list."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(True)
		panel.setCanChooseDirectories_(False)
		panel.setAllowedFileTypes_(["ttf", "otf"])
		result = panel.runModal()
		# Accept both the legacy constant and the modern integer value (1).
		if result not in (_OK, 1):
			return

		url = panel.URL()
		if url is None:
			self._set_status('Could not read selected file path.', error=True)
			return

		path = str(url.path())

		# Refuse to parse unreasonably large files — guards against a crafted
		# or accidental multi-GB binary tying up RoboFont's main thread.
		try:
			size = os.path.getsize(path)
		except OSError as exc:
			self._set_status(f'Cannot access file: {exc}', error=True)
			return
		if size > MAX_FONT_SIZE_BYTES:
			self._set_status(
				f'Font is {size // (1024 * 1024)} MB — exceeds {MAX_FONT_SIZE_BYTES // (1024 * 1024)} MB limit.',
				error=True,
			)
			return

		# Route through the centralised transition so any prior open-font
		# state is cleared and visibility flips in one step.
		self._transition_source_mode(self.SOURCE_FILE)
		self._font_path = path
		self.w.fontPathField.set(path)

		# Default output folder to the font's containing directory.
		self._output_folder = os.path.dirname(path)
		self.w.outputFolderField.set(self._output_folder)

		# Reset dirty flag on new font.
		self._name_dirty = False

		self._load_instances(path)

	def _load_instances(self, path):
		"""Parse fvar named instances from the file-mode font and populate the list."""
		self._instance_names = []
		self.w.instanceList.set([])
		self.w.outputNameField.set('')
		self._set_status('')
		self.w.generateButton.enable(False)
		self._close_font()

		try:
			self._font = TTFont(path)
		except (ttLib.TTLibError, OSError) as exc:
			self._set_status(f'Error loading font: {exc}', error=True)
			log.exception('vf-clamp: error loading font at %r', path)
			return

		# Record baseline revision and cache token at load time so subsequent
		# generations stay monotonic (#61) and survive a fontTools upgrade
		# detection (#58).
		try:
			self._font_revision_baseline = self._font['head'].fontRevision or 0.0 if 'head' in self._font else 0.0
		except Exception:
			self._font_revision_baseline = 0.0
		self._font_cache_token = _fonttools_cache_token()

		if 'fvar' not in self._font:
			self._set_status('Not a variable font — no fvar table found.', error=True)
			return

		fvar = self._font['fvar']
		name_table = self._font['name']
		names = []
		for index, inst in enumerate(fvar.instances):
			label = _get_instance_label(name_table, inst, index)
			names.append(label)

		# Disambiguate any duplicate labels by appending an index suffix.
		seen = {}
		display = []
		for name in names:
			seen[name] = seen.get(name, 0) + 1
			if seen[name] == 1:
				display.append(name)
			else:
				display.append(f'{name} ({seen[name]})')

		self._instance_names = display
		self.w.instanceList.set(display)

		if display:
			# Auto-select the first instance so first-time users see what
			# Generate would produce, instead of a dead-end disabled button.
			try:
				self.w.instanceList.setSelection([0])
			except Exception:
				pass
			# selectionCallback may not fire synchronously after setSelection;
			# update derived UI state explicitly.
			self._on_selection_change(self.w.instanceList)
			self._set_status(f'{len(display)} named instance(s) found.')
		else:
			self._set_status('No named instances found in this font.')

	# -------------------------------------------------------------------------
	# Open-font-mode callbacks
	# -------------------------------------------------------------------------

	def _load_open_font(self, font):
		"""Wire a RoboFont open font into the dialog.

		Finds the sibling designspace, reads instance names cheaply (without
		paying for compileVariableTTF), and populates the list. The actual
		compile happens lazily at Generate time — re-clicking the popup or
		toggling selection doesn't re-compile.
		"""
		self._set_status('Loading open font…')
		ds_path = open_font_core.find_sibling_designspace(font)
		if not ds_path:
			self._set_status(
				'This open font has no sibling .designspace — vf-clamp can only '
				'clamp a designspace (UFO is a single master).',
				error=True,
			)
			return
		if not open_font_core.is_ufo2ft_available():
			self._set_status(
				f'ufo2ft is required: {open_font_core.ufo2ft_import_error()}',
				error=True,
			)
			return

		# Cheap path first — show instance names without compiling.
		names = open_font_core.get_instance_names_from_designspace(ds_path)
		if not names:
			self._set_status('Designspace has no named instances.', error=True)
			return

		# Route through the centralised transition (clears any stale file path,
		# closes the cached TTFont, flips visibility, refreshes the format
		# popup). Assign open-font state AFTER the transition because the
		# transition clears the inactive source's pointer.
		self._transition_source_mode(self.SOURCE_OPEN_FONT)
		self._open_font = font
		self._designspace_path = ds_path
		# Drop any previously cached compiled TTFont — selecting a different
		# open font invalidates it.
		self._close_designspace_ttfont()

		self._instance_names = list(names)
		self.w.instanceList.set(self._instance_names)
		self.w.outputNameField.set('')
		self._name_dirty = False

		# Default output folder to the UFO's parent directory when the user
		# hasn't already chosen one.
		try:
			ufo_path = getattr(font, 'path', '') or ''
		except Exception:
			ufo_path = ''
		if ufo_path and not self._output_folder:
			self._output_folder = os.path.dirname(ufo_path)
			self.w.outputFolderField.set(self._output_folder)

		# Auto-select the first instance so Generate is immediately usable
		# (matches file-mode behaviour).
		try:
			self.w.instanceList.setSelection([0])
		except Exception:
			pass
		self._on_selection_change(self.w.instanceList)
		self._set_status(f'{len(self._instance_names)} instance(s) from {os.path.basename(ds_path)}.')

	# -------------------------------------------------------------------------
	# Shared selection / name callbacks
	# -------------------------------------------------------------------------

	def _on_select_all(self, sender):
		"""Select every instance in the list."""
		count = len(self._instance_names)
		if count == 0:
			return
		try:
			self.w.instanceList.setSelection(list(range(count)))
		except Exception:
			pass
		self._on_selection_change(self.w.instanceList)

	def _on_select_none(self, sender):
		"""Deselect every instance in the list."""
		try:
			self.w.instanceList.setSelection([])
		except Exception:
			pass
		self._on_selection_change(self.w.instanceList)

	def _on_select_invert(self, sender):
		"""Invert the current instance selection."""
		count = len(self._instance_names)
		if count == 0:
			return
		current = set(self.w.instanceList.getSelection())
		inverted = [i for i in range(count) if i not in current]
		try:
			self.w.instanceList.setSelection(inverted)
		except Exception:
			pass
		self._on_selection_change(self.w.instanceList)

	def _on_name_edited(self, sender):
		"""Mark the family name as user-edited so we stop auto-overwriting it."""
		value = (sender.get() or '').strip()
		self._name_dirty = bool(value)

	def _on_selection_change(self, sender):
		"""Update the output name field, hull preview, and Generate button."""
		indices = self.w.instanceList.getSelection()
		if not indices:
			if not self._name_dirty:
				self.w.outputNameField.set('')
			self._refresh_axis_preview([])
			self._update_generate_button()
			return

		# Bounds-check indices against current list length.
		valid = [i for i in indices if 0 <= i < len(self._instance_names)]
		selected = [self._instance_names[i] for i in valid]
		if selected and not self._name_dirty:
			name = compact_name(selected[0], selected[-1])
			self.w.outputNameField.set(name)

		# Render the hull preview — colored axis chips when a font is loaded.
		self._refresh_axis_preview(valid)
		self._update_generate_button()

	def _on_choose_folder(self, sender):
		"""Open a folder picker and store the chosen output directory."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(False)
		panel.setCanChooseDirectories_(True)
		result = panel.runModal()
		if result not in (_OK, 1):
			return

		url = panel.URL()
		if url is None:
			self._set_status('Could not read selected folder path.', error=True)
			return

		self._output_folder = str(url.path())
		self.w.outputFolderField.set(self._output_folder)

	def _on_reveal(self, sender):
		"""Open Finder and select the most recently written output file."""
		if not self._last_output_path or not os.path.exists(self._last_output_path):
			self._set_status('No output file to reveal yet.')
			return
		try:
			from AppKit import NSWorkspace
			NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(
				self._last_output_path, ''
			)
		except Exception as exc:
			log.exception('vf-clamp: reveal-in-finder failed')
			self._set_status(f'Could not reveal file: {exc}', error=True)

	def _on_cancel(self, sender):
		"""Close the dialog without writing output."""
		try:
			self.w.close()
		except (AttributeError, RuntimeError):
			pass

	def _confirm_overwrite(self, path):
		"""Ask the user whether to overwrite an existing file. Returns True to proceed."""
		try:
			from vanilla.dialogs import askYesNo
		except Exception:
			# vanilla.dialogs may not be available — fall back to allowing the write,
			# but log so the user can see what happened.
			log.warning('vf-clamp: vanilla.dialogs.askYesNo unavailable; overwriting %r', path)
			return True
		try:
			result = askYesNo(
				messageText='File already exists',
				informativeText=f'{os.path.basename(path)} already exists in the output folder.\n\nReplace it?',
			)
			# askYesNo returns 1 for Yes, 0 for No.
			return bool(result)
		except Exception:
			log.exception('vf-clamp: overwrite dialog failed')
			return False

	# -------------------------------------------------------------------------
	# Hull preview (colored axis chips)
	# -------------------------------------------------------------------------

	def _set_hull_text(self, text):
		"""Set the hull preview to plain placeholder/error text in muted style."""
		if not _APPKIT_ATTRIB_AVAILABLE:
			try:
				self.w.axisPreview.set(text)
			except Exception:
				pass
			return
		try:
			attr = NSAttributedString.alloc().initWithString_attributes_(
				text,
				{
					NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
					NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()),
				},
			)
			self.w.axisPreview._nsObject.setAttributedStringValue_(attr)
		except Exception:
			try:
				self.w.axisPreview.set(text)
			except Exception:
				pass

	def _hull_for_preview(self, valid_indices):
		"""Compute the axis hull for the preview, dispatching by source mode.

		Returns a dict {tag: (lo, hi)} where lo == hi for pinned axes. Returns
		an empty dict when the hull cannot be computed (no source loaded,
		designspace not yet parsed, etc.).

		Open-font mode reads coordinates straight from the designspace XML
		so we avoid the multi-second compileVariableTTF cost on every
		selection change.
		"""
		if not valid_indices:
			return {}
		try:
			if self._source_mode == self.SOURCE_FILE and self._font is not None:
				raw = compute_hull(self._font, valid_indices)
				return {
					tag: ((c, c) if not isinstance(c, tuple) else c)
					for tag, c in raw.items()
				}
			if self._source_mode == self.SOURCE_OPEN_FONT and self._designspace_path:
				return _hull_from_designspace_instances(
					self._designspace_path,
					self._instance_names,
					valid_indices,
				)
		except Exception as exc:
			log.warning('vf-clamp: hull preview unavailable: %s', exc)
			return {}
		return {}

	# -------------------------------------------------------------------------
	# Hull plot + animated specimen (v1.0.0 — shared with Glyphs plugin via
	# the framework-agnostic hull_plot.py and preview_view.py modules)
	# -------------------------------------------------------------------------

	def _instance_coords_for_indices(self, valid_indices):
		"""Return parallel ``(instances, names, axis_ranges)`` for the source.

		Each ``instance`` is a ``{tag: float}`` coord dict — same shape the
		hull plot expects. ``axis_ranges`` is ``{tag: (min, default, max)}``
		of the full font. Returns ``([], [], {})`` when no source is loaded.

		valid_indices is unused for return shape (we return the *full*
		instance list with coords; the hull plot picks the selected ones
		via setInstances_selectedIndices_onClick_).
		"""
		instances = []
		names = []
		axis_ranges = {}
		try:
			if self._source_mode == self.SOURCE_FILE and self._font is not None:
				if 'fvar' not in self._font:
					return ([], [], {})
				fvar = self._font['fvar']
				name_table = self._font['name']
				for ax in fvar.axes:
					axis_ranges[ax.axisTag] = (
						float(ax.minValue),
						float(ax.defaultValue),
						float(ax.maxValue),
					)
				for idx, inst in enumerate(fvar.instances):
					instances.append({
						tag: float(val)
						for tag, val in inst.coordinates.items()
					})
					names.append(_get_instance_label(name_table, inst, idx))
			elif (
				self._source_mode == self.SOURCE_OPEN_FONT
				and self._designspace_path
			):
				try:
					from fontTools.designspaceLib import DesignSpaceDocument
				except ImportError:
					return ([], [], {})
				doc = DesignSpaceDocument.fromfile(self._designspace_path)
				for ax in doc.axes:
					axis_ranges[ax.tag] = (
						float(ax.minimum),
						float(ax.default),
						float(ax.maximum),
					)
				for inst in doc.instances:
					label = (
						getattr(inst, 'styleName', '')
						or getattr(inst, 'name', '')
						or ''
					).strip()
					if not label:
						continue
					loc = getattr(inst, 'location', None) or {}
					instances.append({
						tag: float(v) for tag, v in loc.items()
					})
					names.append(label)
		except Exception as exc:
			log.warning('vf-clamp: instance coord build failed: %s', exc)
			return ([], [], {})
		return (instances, names, axis_ranges)

	def _axis_color_map(self, axis_tags):
		"""Return ``{tag: (r, g, b)}`` per-axis colours for the hull plot."""
		dark = _is_dark_appearance()
		palette = AXIS_COLORS_DARK if dark else AXIS_COLORS_LIGHT
		default = DEFAULT_AXIS_COLOR_DARK if dark else DEFAULT_AXIS_COLOR_LIGHT
		return {tag: palette.get(tag, default) for tag in axis_tags}

	def _refresh_hull_views(self, valid_indices):
		"""Drive the v1.0.0 design-space plot + animated specimen views.

		Pushes the current hull, axis ranges, instance coordinates, and
		selection mask into the plot view, then sets the same hull on the
		animated specimen and starts / stops its animation timer based on
		whether anything is selected.
		"""
		plot = self._hull_plot_view
		specimen = self._preview_view
		if plot is None and specimen is None:
			return

		hull = self._hull_for_preview(valid_indices) if valid_indices else {}
		instances, _names, axis_ranges = (
			self._instance_coords_for_indices(valid_indices)
		)
		axis_colors = self._axis_color_map(
			list(axis_ranges.keys()) or list(hull.keys()),
		)

		if plot is not None:
			try:
				plot.setHull_axisRanges_axisColors_(
					hull, axis_ranges, axis_colors,
				)
				plot.setInstances_selectedIndices_onClick_(
					instances, list(valid_indices or []), None,
				)
			except (AttributeError, RuntimeError):
				pass

		if specimen is not None:
			try:
				if hull:
					specimen.setHull_(hull)
					specimen.startAnimating()
				else:
					specimen.setHull_({})
					specimen.stopAnimating()
			except (AttributeError, RuntimeError):
				pass

	def _refresh_size_estimate(self, valid_indices):
		"""Update the size-estimate strip beneath the design-space plot."""
		widget = getattr(self.w, 'sizeEstimate', None)
		if widget is None:
			return
		if not valid_indices:
			text = ''
		else:
			n = len(valid_indices)
			parts = [f'{n} instance{"s" if n != 1 else ""}']
			# Quick file-size heuristic (only when we have a source byte count
			# — File mode keeps one; Open Font compiles on demand).
			source_bytes = getattr(self, '_source_size_bytes', None)
			if source_bytes:
				total = max(1, len(self._instance_names))
				ratio = max(0.3, min(1.0, n / total))
				size_kb = int(source_bytes * ratio / 1024)
				parts.insert(0, f'~{size_kb:,} KB')

			masters, axes, pinned = self._count_structural(valid_indices)
			if masters is not None:
				parts.append(
					f'{masters} master{"s" if masters != 1 else ""}',
				)
			if axes is not None and axes > 0:
				if pinned:
					parts.append(f'{axes} ax · {pinned} pinned')
				else:
					parts.append(f'{axes} ax')
			text = '  ·  '.join(parts)
		try:
			widget.set(text)
		except (AttributeError, RuntimeError):
			pass

	def _count_structural(self, valid_indices):
		"""Return ``(masters, axes, pinned)`` for the current selection."""
		try:
			hull = self._hull_for_preview(valid_indices)
		except Exception:
			hull = {}
		if not hull:
			return (None, None, None)
		axes = len(hull)
		pinned = sum(1 for lo, hi in hull.values() if lo == hi)
		# Master count: only computable for File source (TTFont gvar /
		# fvar geometry). Open Font is a designspace + UFOs; counting
		# "masters in hull" would mean parsing the designspace XML and
		# checking each source location, which is doable but adds latency
		# on every selection change. Skip for v1.0.0; the axes + pinned
		# counts are the most informative anyway.
		masters = None
		try:
			if (
				self._source_mode == self.SOURCE_OPEN_FONT
				and self._designspace_path
			):
				try:
					from fontTools.designspaceLib import DesignSpaceDocument
				except ImportError:
					return (None, axes, pinned)
				doc = DesignSpaceDocument.fromfile(self._designspace_path)
				count = 0
				for src in doc.sources:
					loc = getattr(src, 'location', None) or {}
					ok = True
					for tag, (lo, hi) in hull.items():
						val = loc.get(tag)
						if val is None:
							continue
						if not (lo <= float(val) <= hi):
							ok = False
							break
					if ok:
						count += 1
				masters = count
		except Exception:
			masters = None
		return (masters, axes, pinned)

	def _refresh_axis_preview(self, valid_indices):
		"""Render the axis hull as colored ■-prefixed chips, one axis per line.

		v1.0.0 also drives the new design-space plot and animated specimen
		when they're mounted. The chips TextBox stays in the call path as
		a fallback for empty/error states and so the existing layout code
		doesn't have to change.
		"""
		# v1.0.0: drive the design-space plot + animated specimen if they
		# mounted at build time. _refresh_hull_views handles its own empty
		# state, so we always call it — even when valid_indices is [].
		self._refresh_hull_views(valid_indices)
		self._refresh_size_estimate(valid_indices)

		if not valid_indices:
			self._set_hull_text('(select instances to preview)')
			return

		hull = self._hull_for_preview(valid_indices)
		if not hull:
			self._set_hull_text('(no axes)')
			return

		if not _APPKIT_ATTRIB_AVAILABLE:
			parts = []
			for tag, (lo, hi) in hull.items():
				a = f'{lo:g}'
				b = f'{hi:g}'
				parts.append(f'{tag} {a}' if a == b else f'{tag} {a}–{b}')
			try:
				self.w.axisPreview.set('  ·  '.join(parts))
			except Exception:
				pass
			return

		# Build an attributed string: per-axis line is "■  TAG  lo–hi" where the
		# leading ■ is colored by the per-axis palette.
		attr = NSMutableAttributedString.alloc().init()
		small_font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())
		mono_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
			NSFont.smallSystemFontSize(),
			0.0,  # NSFontWeightRegular
		)
		muted = NSColor.secondaryLabelColor()
		label = NSColor.labelColor()

		first = True
		for tag, (lo, hi) in hull.items():
			if not first:
				attr.appendAttributedString_(
					NSAttributedString.alloc().initWithString_attributes_('\n', {})
				)
			first = False
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					'■  ',
					{
						NSForegroundColorAttributeName: _nscolor_for_axis(tag),
						NSFontAttributeName: small_font,
					},
				)
			)
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					f'{tag}',
					{
						NSForegroundColorAttributeName: label,
						NSFontAttributeName: small_font,
					},
				)
			)
			a = f'{lo:g}'
			b = f'{hi:g}'
			range_text = f'  pinned at {a}' if a == b else f'  {a} – {b}'
			attr.appendAttributedString_(
				NSAttributedString.alloc().initWithString_attributes_(
					range_text,
					{
						NSForegroundColorAttributeName: muted,
						NSFontAttributeName: mono_font,
					},
				)
			)

		try:
			self.w.axisPreview._nsObject.setAttributedStringValue_(attr)
		except Exception:
			# Fall back to plain text if attributed rendering fails.
			parts = []
			for tag, (lo, hi) in hull.items():
				a = f'{lo:g}'
				b = f'{hi:g}'
				parts.append(f'{tag} {a}' if a == b else f'{tag} {a}–{b}')
			try:
				self.w.axisPreview.set('  ·  '.join(parts))
			except Exception:
				pass

		# Accessibility: VoiceOver would otherwise read every "■" as
		# "black square". Provide a clean axis summary instead — the colored
		# chip is a redundant visual cue (WCAG 1.4.1 prohibits color-only
		# encoding).
		ax_parts = []
		for tag, (lo, hi) in hull.items():
			a = f'{lo:g}'
			b = f'{hi:g}'
			if a == b:
				ax_parts.append(f'{tag} pinned at {a}')
			else:
				ax_parts.append(f'{tag} {a} to {b}')
		ax_summary = '; '.join(ax_parts) if ax_parts else 'No axes'
		try:
			self.w.axisPreview._nsObject.setAccessibilityLabel_('Axis hull')
			self.w.axisPreview._nsObject.setAccessibilityValue_(ax_summary)
		except (AttributeError, RuntimeError):
			pass

	# -------------------------------------------------------------------------
	# Generate dispatch
	# -------------------------------------------------------------------------

	def _collect_generate_inputs(self):
		"""Read every dialog input needed by Generate; return params or an error.

		Returns ``(params, error_message)`` where exactly one element is
		non-None. ``params`` is a dict with keys ``selected_indices``,
		``family_name``, ``format_label``, ``output_path`` ready to hand to a
		dispatcher.

		Extracted from `_on_generate` (mirrors Glyphs plugin _collect_generate_inputs)
		so the dispatcher is purely two-line: collect-or-error, then dispatch.
		"""
		if self._source_mode == self.SOURCE_FILE:
			if not self._font_path or self._font is None:
				return None, 'No font selected.'
		else:
			if self._open_font is None or not self._designspace_path:
				return None, 'No open font selected.'

		indices = self.w.instanceList.getSelection()
		if not indices:
			return None, 'Select at least one instance.'

		valid_indices = [i for i in indices if 0 <= i < len(self._instance_names)]
		if not valid_indices:
			return None, 'Selection is no longer valid — choose instances again.'

		family_name = self.w.outputNameField.get().strip()
		if not family_name:
			return None, 'Output family name is required.'

		# Resolve the chosen format via the central registry. UFO is a
		# controller-local sentinel — it isn't in formats.py because UFO write
		# does not go through fontTools.
		format_items = self.w.formatPopUp.getItems()
		format_index = self.w.formatPopUp.get()
		if 0 <= format_index < len(format_items):
			format_label = format_items[format_index]
		else:
			format_label = self.BINARY_FORMATS[0]

		# Folder fallback: chosen → file source dir → UFO source dir → ~/Desktop.
		output_folder = self._output_folder
		if not output_folder:
			if self._source_mode == self.SOURCE_FILE and self._font_path:
				output_folder = os.path.dirname(self._font_path)
			elif self._source_mode == self.SOURCE_OPEN_FONT and self._open_font is not None:
				try:
					ufo_path = getattr(self._open_font, 'path', '') or ''
				except Exception:
					ufo_path = ''
				output_folder = os.path.dirname(ufo_path) if ufo_path else os.path.expanduser('~/Desktop')
			else:
				output_folder = os.path.expanduser('~/Desktop')

		# Resolve extension. UFO is special-cased; everything else routes
		# through formats.py. The "inherits from source" case in file mode
		# uses the original behaviour; in open-font mode the source is a
		# .designspace (no SFNT extension) so we default to .ttf.
		if format_label == self.UFO_FORMAT_LABEL:
			ext = '.ufo'
		elif formats.inherits_ext(format_label):
			ext = _resolve_output_extension('', self._font_path)
		else:
			ext = formats.extension_for(format_label)

		safe_name = sanitize_filename(family_name)
		output_path = os.path.join(output_folder, f'{safe_name}{ext}')

		# Defence-in-depth: refuse to write outside the chosen output folder.
		# Compare via os.path.commonpath rather than direct equality so a
		# symlinked output folder (real path resolves elsewhere) doesn't trip
		# a false positive (resolves #60). We require the resolved output dir
		# to live *inside* the resolved chosen folder — equality is the most
		# common case but not the only valid one.
		try:
			resolved_out_dir = os.path.realpath(os.path.dirname(output_path))
			resolved_chosen = os.path.realpath(output_folder)
			common = os.path.commonpath([resolved_out_dir, resolved_chosen])
		except ValueError:
			# commonpath raises on mixed drives / empty paths — treat as unsafe.
			return None, 'Refusing to write outside selected output folder.'
		if common != resolved_chosen:
			return None, 'Refusing to write outside selected output folder.'

		return {
			'selected_indices': valid_indices,
			'family_name': family_name,
			'format_label': format_label,
			'output_path': output_path,
		}, None

	def _on_generate(self, sender):
		"""Top-level dispatch: validate inputs, then route to the right source path."""
		if self._generating:
			return
		params, err = self._collect_generate_inputs()
		if err is not None:
			self._set_status(err, error=True)
			return

		self._generating = True
		self.w.generateButton.enable(False)
		self.w.revealButton.enable(False)
		self._set_status('Processing…')

		try:
			if self._source_mode == self.SOURCE_OPEN_FONT:
				self._generate_from_open_font(**params)
			else:
				self._generate_from_file(**params)
		finally:
			self._generating = False
			self._update_generate_button()

	def _generate_from_file(self, *, selected_indices, family_name, format_label, output_path):
		"""File-source path — runs fontTools instancer against the disk file."""
		# Invalidate the cached TTFont if fontTools was reloaded between clicks
		# — guards against mixing old-build instancer with new-build name
		# patching on the same in-memory font (resolves #58).
		current_token = _fonttools_cache_token()
		if current_token != self._font_cache_token and self._font_path:
			log.info('vf-clamp: fontTools build changed; reloading cached font')
			self._close_font()
			self._font_cache_token = current_token
			try:
				self._font = TTFont(self._font_path)
				if 'head' in self._font:
					self._font_revision_baseline = self._font['head'].fontRevision or 0.0
			except Exception as exc:
				self._set_status(f'Error reloading font: {exc}', error=True)
				return

		flavor = formats.flavor_for(format_label)

		# Overwrite confirmation if file already exists.
		overwrite = False
		if os.path.exists(output_path):
			if not self._confirm_overwrite(output_path):
				self._set_status('Generation cancelled — file already exists.')
				return
			overwrite = True

		try:
			info = produce_restricted_vf(
				self._font,
				selected_indices,
				family_name,
				output_path,
				flavor=flavor,
				overwrite=overwrite,
				revision_baseline=self._font_revision_baseline,
			)
		except FileExistsError as exc:
			self._set_status(f'File already exists: {exc}', error=True)
			return
		except (ValueError, AssertionError, ttLib.TTLibError) as exc:
			self._set_status(str(exc), error=True)
			log.exception('vf-clamp: generation error')
			return
		except Exception as exc:
			self._set_status(f'Unexpected error: {exc}\nSee Python Output for details.', error=True)
			log.error('vf-clamp: unexpected error during generation')
			traceback.print_exc()
			return

		self._surface_generation_notes(info, output_path)

	def _generate_from_open_font(self, *, selected_indices, family_name, format_label, output_path):
		"""Open-font-source path — compile designspace → TTFont → existing pipeline.

		For UFO output we refuse — a UFO is a single master, not a VF.
		"compute_hull" against a single master would always produce a pinned
		hull on every axis (i.e. a static font masquerading as a UFO), which
		is misleading. Better to surface a clear refusal than write a file
		the user can't trust.
		"""
		if format_label == self.UFO_FORMAT_LABEL:
			self._set_status(
				'UFO output is not supported — UFO is a single master, not a variable font. '
				'Pick TTF/OTF/WOFF/WOFF2 to produce a clamped variable font.',
				error=True,
			)
			return

		self._set_status('Compiling designspace…')
		try:
			ttfont = open_font_core.compile_designspace_to_ttfont(self._designspace_path)
		except RuntimeError as exc:
			self._set_status(f'Compile failed: {exc}', error=True)
			log.exception('vf-clamp: designspace compile failed')
			return
		except Exception as exc:
			self._set_status(f'Unexpected compile error: {exc}', error=True)
			log.error('vf-clamp: unexpected error during designspace compile')
			traceback.print_exc()
			return

		# Cache for accessibility/debugging; cleared after save.
		self._cached_ttfont_from_designspace = ttfont

		# Selection from the dialog list points at designspace instance order,
		# which after compileVariableTTF lines up with fvar.instances order.
		# Confirm that count matches; if not, refuse rather than write a
		# misleading file.
		try:
			compiled_count = len(ttfont['fvar'].instances) if 'fvar' in ttfont else 0
		except Exception:
			compiled_count = 0
		if compiled_count != len(self._instance_names):
			self._set_status(
				f'Compiled font has {compiled_count} instance(s) but the dialog shows '
				f'{len(self._instance_names)}. Re-open the font to refresh.',
				error=True,
			)
			self._close_designspace_ttfont()
			return

		flavor = formats.flavor_for(format_label)

		overwrite = False
		if os.path.exists(output_path):
			if not self._confirm_overwrite(output_path):
				self._set_status('Generation cancelled — file already exists.')
				self._close_designspace_ttfont()
				return
			overwrite = True

		try:
			# Open-font path compiles fresh each Generate so no baseline carry-over is needed.
			compiled_baseline = ttfont['head'].fontRevision if 'head' in ttfont else None
			info = produce_restricted_vf(
				ttfont,
				selected_indices,
				family_name,
				output_path,
				flavor=flavor,
				overwrite=overwrite,
				revision_baseline=compiled_baseline,
			)
		except FileExistsError as exc:
			self._set_status(f'File already exists: {exc}', error=True)
			return
		except (ValueError, AssertionError, ttLib.TTLibError) as exc:
			self._set_status(str(exc), error=True)
			log.exception('vf-clamp: open-font generation error')
			return
		except Exception as exc:
			self._set_status(f'Unexpected error: {exc}\nSee Python Output for details.', error=True)
			log.error('vf-clamp: unexpected error during open-font generation')
			traceback.print_exc()
			return
		finally:
			# Always release the compiled TTFont — it lives in memory for the
			# lifetime of one Generate, no longer.
			self._close_designspace_ttfont()

		self._surface_generation_notes(info, output_path)

	def _surface_generation_notes(self, info, output_path):
		"""Format the post-generate info dict into a multi-line status message."""
		notes = [f'Saved → {os.path.basename(output_path)}']
		for tag, original, clamped in info.get('clamped_defaults') or []:
			notes.append(f'Warning: {tag} default clamped {original} → {clamped}')
		pruned = info.get('pruned_instances') or 0
		if pruned:
			notes.append(f'Pruned {pruned} fvar instance(s) outside range.')
		pruned_stat = info.get('pruned_stat_axis_values') or 0
		if pruned_stat:
			notes.append(f'Pruned {pruned_stat} STAT AxisValue(s).')
		if info.get('stripped_dsig'):
			notes.append('Removed DSIG (invalidated by edit).')

		self._set_status('\n'.join(notes))
		self._last_output_path = output_path
		self.w.revealButton.enable(True)
		try:
			self.w.revealButton.show(True)
		except Exception:
			pass


def _hull_from_designspace_instances(designspace_path, instance_names, valid_indices):
	"""Return {tag: (lo, hi)} computed straight from designspace instance coordinates.

	Avoids the multi-second compileVariableTTF cost so the hull preview stays
	responsive in open-font mode. ``instance_names`` is the dialog's display
	list (parallel to ``designspace.instances``); ``valid_indices`` are
	positional indices into that list.
	"""
	try:
		from fontTools.designspaceLib import DesignSpaceDocument
	except ImportError:
		return {}
	try:
		doc = DesignSpaceDocument.fromfile(designspace_path)
	except Exception:
		return {}

	# Build a list of instance locations in the same order as the dialog's
	# instance_names. The dialog's name resolution mirrors
	# get_instance_names_from_designspace (skip empty labels), so we replicate
	# that filter here to keep indices aligned.
	ordered_locations = []
	for inst in doc.instances:
		label = (getattr(inst, 'styleName', '') or getattr(inst, 'name', '') or '').strip()
		if not label:
			continue
		ordered_locations.append(getattr(inst, 'location', None) or {})

	hull = {}
	for key in valid_indices:
		if not (0 <= key < len(ordered_locations)):
			continue
		coords = ordered_locations[key]
		for tag, val in coords.items():
			if tag not in hull:
				hull[tag] = [val, val]
			else:
				hull[tag][0] = min(hull[tag][0], val)
				hull[tag][1] = max(hull[tag][1], val)
	return {tag: (lo, hi) for tag, (lo, hi) in hull.items()}
