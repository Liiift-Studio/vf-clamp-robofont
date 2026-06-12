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

def _check_fonttools_version():
	"""Warn at import time if RoboFont bundles an older fontTools than we support."""
	try:
		from fontTools import __version__ as ft_ver
		parts = tuple(int(p) for p in ft_ver.split('.')[:3])
		if parts < MIN_FONTTOOLS_VERSION:
			warnings.warn(
				f'vf-clamp: fontTools {ft_ver} is older than the minimum tested version '
				f'{".".join(str(p) for p in MIN_FONTTOOLS_VERSION)}; restriction ranges may misbehave.',
				RuntimeWarning,
			)
	except Exception:
		# Version string parsing or import failure — non-fatal; skip the check.
		pass

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

	Canonical TypeScript implementation: @liiift-studio/vf-clamp src/core/utils.ts compactName()
	Duplicate also exists in vf-clamp-glyphs plugin.py and vf-clamp-vscode panel.ts webview.
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
	"""
	removed = 0
	if 'STAT' not in font:
		return removed
	stat = font['STAT'].table
	axis_value_array = getattr(stat, 'AxisValueArray', None)
	design_axis_record = getattr(stat, 'DesignAxisRecord', None)
	if axis_value_array is None or design_axis_record is None:
		return removed
	# Build map: design-axis-index → axis tag
	axes = list(design_axis_record.Axis)
	index_to_tag = {i: ax.AxisTag for i, ax in enumerate(axes)}

	def _value_in_hull(av):
		"""Return True if this AxisValue refers to an in-hull axis value."""
		fmt = getattr(av, 'Format', None)
		# Format 1/2/3 reference a single axis via AxisIndex.
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
				# Nominal must be in range; min/max may extend outside, drop conservatively.
				return lo <= av.NominalValue <= hi
			return True
		if fmt == 4:
			# Format 4 references multiple axes via AxisValueRecord list.
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
	axis_value_array.AxisValue = kept
	return removed


def _strip_dsig(font):
	"""Remove the DSIG table — any modification invalidates a digital signature.

	Returns True if a DSIG was removed.
	"""
	if 'DSIG' in font:
		del font['DSIG']
		return True
	return False


def _bump_font_revision(font):
	"""Bump head.fontRevision by 0.001 so font caches differentiate the derivative
	from its source. No-op if the head table is missing.
	"""
	if 'head' not in font:
		return
	head = font['head']
	head.fontRevision = round((head.fontRevision or 0.0) + 0.001, 3)


def _resolve_output_extension(ext_override, source_path):
	"""Return the output extension. When ext_override is '' inherit from source.

	`source_path` may be None in open-font mode — falls back to .ttf in that case
	because ufo2ft.compileVariableTTF always produces a TTF binary.
	"""
	if ext_override:
		return ext_override
	if not source_path:
		return '.ttf'
	src_ext = os.path.splitext(source_path)[1].lower()
	if src_ext in ('.ttf', '.otf'):
		return src_ext
	return '.ttf'


def produce_restricted_vf(font, selected_keys, family_name, output_path, flavor=None, overwrite=False):
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
	_bump_font_revision(partial)
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

	# Window dimensions
	WINDOW_WIDTH = 560
	WINDOW_HEIGHT = 480
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
	HULL_H = 64

	def __init__(self):
		"""Initialise the controller window and all UI elements."""
		# File-mode state
		self._font_path = None
		self._font = None  # cached TTFont (file-mode)

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

		# --- Row 5: Hull preview (colored axis chips) ------------------------
		win.hullLabel = self._right_label((PAD, y + 2, LABEL_COL_W, LABEL_H), 'Hull:')
		win.axisPreview = vanilla.TextBox(
			(CONTROL_X, y, -PAD, HULL_H),
			'(select instances to preview)',
			sizeStyle='small',
			selectable=True,
		)
		# vanilla.TextBox wraps NSTextField in single-line mode by default;
		# the hull preview puts one axis per line so we need wrapping back on
		# or '\n' chars render as glyph-not-found boxes.
		try:
			cell = win.axisPreview._nsObject.cell()
			cell.setUsesSingleLineMode_(False)
			cell.setWraps_(True)
		except (AttributeError, RuntimeError):
			pass
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
		"""Enable Generate only when a source is loaded and ≥1 instance is selected."""
		indices = self.w.instanceList.getSelection()
		source_loaded = (
			(self._source_mode == self.SOURCE_FILE and self._font_path is not None)
			or (self._source_mode == self.SOURCE_OPEN_FONT and self._open_font is not None)
		)
		enabled = bool(source_loaded and indices) and not self._generating
		self.w.generateButton.enable(enabled)

	def _close_font(self):
		"""Close any cached file-mode TTFont and release the file handle."""
		if self._font is not None:
			try:
				self._font.close()
			except Exception:
				pass
			self._font = None

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

	def _refresh_axis_preview(self, valid_indices):
		"""Render the axis hull as colored ■-prefixed chips, one axis per line."""
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
		resolved_out_dir = os.path.realpath(os.path.dirname(output_path))
		resolved_chosen = os.path.realpath(output_folder)
		if resolved_out_dir != resolved_chosen:
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
			info = produce_restricted_vf(
				ttfont,
				selected_indices,
				family_name,
				output_path,
				flavor=flavor,
				overwrite=overwrite,
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
