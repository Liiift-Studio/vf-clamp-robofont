# vf-clamp controller — vanilla UI for generating restricted variable fonts from named instance ranges.

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

from fontTools import ttLib
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

log = logging.getLogger('vfClamp')


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


# Output format → (extension, fontTools flavor)
# flavor=None  → write SFNT (TTF or OTF, determined by input sfntVersion)
# flavor='woff' / 'woff2' → web font compression
FORMAT_OPTIONS = {
	'TTF/OTF (original)': ('', None),
	'WOFF': ('.woff', 'woff'),
	'WOFF2': ('.woff2', 'woff2'),
}

# Ordered list for indexing into the popup.
FORMAT_LABELS = list(FORMAT_OPTIONS.keys())


def _resolve_output_extension(ext_override, source_path):
	"""Return the output extension. When ext_override is '' inherit from source."""
	if ext_override:
		return ext_override
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
	"""Floating window controller for generating restricted variable fonts."""

	# Window dimensions
	WINDOW_WIDTH = 540
	WINDOW_HEIGHT = 420
	MAX_WIDTH = 1200
	MAX_HEIGHT = 1200

	def __init__(self):
		"""Initialise the controller window and all UI elements."""
		self._font_path = None
		self._font = None  # cached TTFont
		self._instance_names = []
		self._output_folder = None
		# Dirty flag: True once the user has typed a custom family name, so
		# selection changes no longer overwrite it.
		self._name_dirty = False
		# Re-entrancy guard so a second click on Generate during a synchronous
		# run is a no-op.
		self._generating = False

		w = self.WINDOW_WIDTH
		h = self.WINDOW_HEIGHT

		self.w = vanilla.FloatingWindow(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, h),
			maxSize=(self.MAX_WIDTH, self.MAX_HEIGHT),
			autosaveName='vfClampMainWindow',
		)

		# -- Font file section --------------------------------------------------
		self.w.fontLabel = vanilla.TextBox(
			(16, 16, -16, 17),
			'Variable Font File',
		)
		self.w.fontPathField = vanilla.EditText(
			(16, 36, -110, 22),
			placeholder='No font selected…',
			readOnly=True,
		)
		self.w.selectFontButton = vanilla.Button(
			(-102, 36, 86, 22),
			'Select Font…',
			callback=self._on_select_font,
		)

		# -- Instance list section ----------------------------------------------
		self.w.instanceLabel = vanilla.TextBox(
			(16, 72, -120, 17),
			'Named Instances (select one or more)',
		)
		self.w.selectAllButton = vanilla.Button(
			(-108, 70, 92, 20),
			'Select All',
			callback=self._on_select_all,
		)
		# List sized with negative bottom anchor so it expands when the window
		# resizes — fonts with 20+ instances (Recursive, Roboto Flex) need room.
		self.w.instanceList = vanilla.List(
			(16, 92, -16, -174),
			[],
			allowsMultipleSelection=True,
			selectionCallback=self._on_selection_change,
		)

		# -- Output name section ------------------------------------------------
		self.w.outputNameLabel = vanilla.TextBox(
			(16, -166, -16, 17),
			'Output Family Name',
		)
		self.w.outputNameField = vanilla.EditText(
			(16, -146, -16, 22),
			placeholder='Auto-generated from selection…',
			callback=self._on_name_edited,
		)

		# -- Format section -----------------------------------------------------
		self.w.formatLabel = vanilla.TextBox(
			(16, -110, 100, 17),
			'Format',
		)
		self.w.formatPopUp = vanilla.PopUpButton(
			(16, -92, 180, 22),
			FORMAT_LABELS,
		)

		# -- Output folder section ----------------------------------------------
		self.w.outputFolderLabel = vanilla.TextBox(
			(210, -110, -16, 17),
			'Output Folder',
		)
		self.w.outputFolderField = vanilla.EditText(
			(210, -92, -110, 22),
			placeholder='Same folder as font…',
			readOnly=True,
		)
		self.w.chooseFolderButton = vanilla.Button(
			(-102, -92, 86, 22),
			'Choose Folder…',
			callback=self._on_choose_folder,
		)

		# -- Generate / status --------------------------------------------------
		self.w.generateButton = vanilla.Button(
			(16, -40, 120, 22),
			'Generate',
			callback=self._on_generate,
		)
		# Default button: bind to Return key so users can hit Enter to generate.
		self.w.setDefaultButton(self.w.generateButton)
		# Disable until a font is loaded and at least one instance is selected.
		self.w.generateButton.enable(False)

		self.w.revealButton = vanilla.Button(
			(144, -40, 120, 22),
			'Reveal in Finder',
			callback=self._on_reveal,
		)
		self.w.revealButton.enable(False)
		self._last_output_path = None

		# Multi-line status display so long fontTools tracebacks remain readable
		# instead of being clipped to a single TextBox row.
		self.w.statusLabel = vanilla.TextEditor(
			(280, -52, -16, 36),
			'',
			readOnly=True,
		)

		self.w.open()

	# -------------------------------------------------------------------------
	# Internal helpers
	# -------------------------------------------------------------------------

	def _set_status(self, message):
		"""Set status text; safe to call with multi-line content."""
		self.w.statusLabel.set(message)

	def _update_generate_button(self):
		"""Enable Generate only when a font and at least one instance are ready."""
		indices = self.w.instanceList.getSelection()
		enabled = bool(self._font_path and indices) and not self._generating
		self.w.generateButton.enable(enabled)

	def _close_font(self):
		"""Close any cached TTFont and release the file handle."""
		if self._font is not None:
			try:
				self._font.close()
			except Exception:
				pass
			self._font = None

	# -------------------------------------------------------------------------
	# Callbacks
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
			self._set_status('Could not read selected file path.')
			return

		path = str(url.path())

		# Refuse to parse unreasonably large files — guards against a crafted
		# or accidental multi-GB binary tying up RoboFont's main thread.
		try:
			size = os.path.getsize(path)
		except OSError as exc:
			self._set_status(f'Cannot access file: {exc}')
			return
		if size > MAX_FONT_SIZE_BYTES:
			self._set_status(
				f'Font is {size // (1024 * 1024)} MB — exceeds {MAX_FONT_SIZE_BYTES // (1024 * 1024)} MB limit.'
			)
			return

		self._font_path = path
		self.w.fontPathField.set(path)

		# Default output folder to the font's containing directory.
		self._output_folder = os.path.dirname(path)
		self.w.outputFolderField.set(self._output_folder)

		# Reset dirty flag on new font.
		self._name_dirty = False

		self._load_instances(path)

	def _load_instances(self, path):
		"""Parse fvar named instances from the font and populate the list."""
		self._instance_names = []
		self.w.instanceList.set([])
		self.w.outputNameField.set('')
		self._set_status('')
		self.w.generateButton.enable(False)
		self._close_font()

		try:
			self._font = TTFont(path)
		except (ttLib.TTLibError, OSError) as exc:
			self._set_status(f'Error loading font: {exc}')
			log.exception('vf-clamp: error loading font at %r', path)
			return

		if 'fvar' not in self._font:
			self._set_status('Not a variable font — no fvar table found.')
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

	def _on_name_edited(self, sender):
		"""Mark the family name as user-edited so we stop auto-overwriting it."""
		# Only treat as dirty if non-empty — clearing the field re-enables auto.
		value = (sender.get() or '').strip()
		self._name_dirty = bool(value)

	def _on_selection_change(self, sender):
		"""Update the output name field, hull preview, and Generate button."""
		indices = self.w.instanceList.getSelection()
		if not indices:
			# Don't clobber a user-edited name on empty selection.
			if not self._name_dirty:
				self.w.outputNameField.set('')
			self._update_generate_button()
			return

		# Bounds-check indices against current list length.
		valid = [i for i in indices if 0 <= i < len(self._instance_names)]
		selected = [self._instance_names[i] for i in valid]
		if selected and not self._name_dirty:
			name = compact_name(selected[0], selected[-1])
			self.w.outputNameField.set(name)

		# Preview the computed hull so users know what axis range they're about
		# to generate before they click Generate.
		if self._font is not None and valid:
			try:
				hull = compute_hull(self._font, valid)
				parts = []
				for tag, c in hull.items():
					if isinstance(c, tuple):
						parts.append(f'{tag} {c[0]:g}-{c[1]:g}')
					else:
						parts.append(f'{tag} {c:g} (pinned)')
				if parts:
					self._set_status('Will generate: ' + ', '.join(parts))
			except Exception:
				# Preview is best-effort; don't break selection UI on failure.
				pass

		self._update_generate_button()

	def _on_choose_folder(self, sender):
		"""Open a folder picker and store the chosen output directory."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(False)
		panel.setCanChooseDirectories_(True)
		result = panel.runModal()
		# Accept both the legacy constant and the modern integer value (1).
		if result not in (_OK, 1):
			return

		url = panel.URL()
		if url is None:
			self._set_status('Could not read selected folder path.')
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
			self._set_status(f'Could not reveal file: {exc}')

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

	def _on_generate(self, sender):
		"""Validate inputs and call produce_restricted_vf to write the output file."""
		if self._generating:
			return
		self._generating = True
		self.w.generateButton.enable(False)
		self.w.revealButton.enable(False)
		self._set_status('Processing…')

		try:
			# -- Validate ----------------------------------------------------------
			if not self._font_path or self._font is None:
				self._set_status('No font selected.')
				return

			indices = self.w.instanceList.getSelection()
			if not indices:
				self._set_status('Select at least one instance.')
				return

			# Bounds-check selection.
			valid_indices = [i for i in indices if 0 <= i < len(self._instance_names)]
			if not valid_indices:
				self._set_status('Selection is no longer valid — choose instances again.')
				return

			family_name = self.w.outputNameField.get().strip()
			if not family_name:
				self._set_status('Output family name is required.')
				return

			output_folder = self._output_folder or os.path.dirname(self._font_path)

			# Use the selected index to look up the format label + (ext, flavor).
			format_index = self.w.formatPopUp.get()
			format_label = FORMAT_LABELS[format_index]
			ext_override, flavor = FORMAT_OPTIONS.get(format_label, ('', None))
			ext = _resolve_output_extension(ext_override, self._font_path)

			# Sanitize family name for use as a filename component.
			safe_name = sanitize_filename(family_name)
			output_path = os.path.join(output_folder, f'{safe_name}{ext}')

			# Guard against accidentally writing outside the chosen output folder
			# (would only matter if sanitize_filename ever permitted slashes, but
			# keep the check as defence-in-depth).
			resolved_out_dir = os.path.realpath(os.path.dirname(output_path))
			resolved_chosen = os.path.realpath(output_folder)
			if resolved_out_dir != resolved_chosen:
				self._set_status('Refusing to write outside selected output folder.')
				return

			# Overwrite confirmation if file already exists.
			overwrite = False
			if os.path.exists(output_path):
				if not self._confirm_overwrite(output_path):
					self._set_status('Generation cancelled — file already exists.')
					return
				overwrite = True

			# -- Generate ----------------------------------------------------------
			info = produce_restricted_vf(
				self._font,
				valid_indices,
				family_name,
				output_path,
				flavor=flavor,
				overwrite=overwrite,
			)

			# Surface non-fatal warnings from the post-processing pass.
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

		except FileExistsError as exc:
			self._set_status(f'File already exists: {exc}')
		except (ValueError, AssertionError, ttLib.TTLibError) as exc:
			self._set_status(f'Error: {exc}')
			log.exception('vf-clamp: generation error')
		except Exception as exc:
			# Last-resort handler — full traceback to console for support.
			self._set_status(f'Unexpected error: {exc}\nSee Python Output for details.')
			log.error('vf-clamp: unexpected error during generation')
			traceback.print_exc()
		finally:
			self._generating = False
			self._update_generate_button()
