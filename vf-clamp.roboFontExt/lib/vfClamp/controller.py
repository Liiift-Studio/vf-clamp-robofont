# vf-clamp controller — vanilla UI for generating restricted variable fonts from named instance ranges.

import os
import re

import vanilla

# NSModalResponseOK (1) is the modern constant; NSFileHandlingPanelOKButton is the
# legacy alias that may not exist in newer AppKit/macOS SDKs. Import both defensively.
try:
	from AppKit import NSOpenPanel, NSModalResponseOK as _OK
except ImportError:
	try:
		from AppKit import NSOpenPanel, NSFileHandlingPanelOKButton as _OK
	except ImportError:
		from AppKit import NSOpenPanel
		_OK = 1  # raw integer fallback

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def compute_hull(font, selected_names):
	"""Compute axis hull (min/max per axis) across selected named instances.

	Returns a dict mapping axis tag → pin value (number) when min == max,
	or (min, max) tuple when a range is needed. Axes not touched by the
	selected instances are omitted so instancer leaves them at full range.
	"""
	fvar = font['fvar']
	name_table = font['name']
	all_insts = {
		name_table.getDebugName(inst.subfamilyNameID): dict(inst.coordinates)
		for inst in fvar.instances
	}
	hull = {}
	for name in selected_names:
		if name not in all_insts:
			continue
		for tag, val in all_insts[name].items():
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


def patch_name_table(font, family_name):
	"""Update name table so restricted VF reflects its instance range.

	Updates nameID 1 (Family), 4 (Full Name), 6 (PostScript Name).
	nameID 16 (Typographic Family) and 25 (Variations PS Name Prefix) are
	only updated when already present in the font (older fonts may lack them).
	PostScript name is kept ASCII-safe as required by the spec.
	"""
	# PostScript name: strip everything except A-Za-z0-9 and hyphens.
	ps_name = re.sub(r'[^A-Za-z0-9-]', '', family_name.replace(' ', '-'))
	name_table = font['name']
	existing_ids = {r.nameID for r in name_table.names}
	updates = {1: family_name, 4: family_name, 6: ps_name}
	if 16 in existing_ids:
		updates[16] = family_name
	if 25 in existing_ids:
		updates[25] = ps_name
	for record in name_table.names:
		if record.nameID not in updates:
			continue
		value = updates[record.nameID]
		if record.platformID == 3:
			# Windows platform: bytes are always UTF-16BE even for ASCII content.
			record.string = value.encode('utf-16-be')
		elif record.platformID == 1:
			# Mac/platform 1: mac_roman; fall back to ASCII with replacement.
			try:
				record.string = value.encode('mac_roman')
			except Exception:
				record.string = value.encode('ascii', errors='replace')

	# Ensure Windows (platformID 3) records exist — older fonts may only have Mac records.
	# Modern renderers (Windows, browsers) depend on platformID 3 entries.
	for name_id, value in updates.items():
		has_windows = any(r.nameID == name_id and r.platformID == 3 for r in name_table.names)
		if not has_windows:
			name_table.setName(value, name_id, 3, 1, 0x0409)


def compact_name(first, last):
	"""Strip shared word prefix/suffix — 'Inter Light' + 'Inter Bold' → 'Inter Light-Bold'."""
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
	"""Replace filesystem-unsafe characters with hyphens and strip leading/trailing hyphens."""
	safe = re.sub(r'[/\\:*?"<>|]', '-', name)
	safe = safe.strip('-').strip()
	return safe or 'output'


def produce_restricted_vf(font_path, selected_names, family_name, output_path):
	"""Produce one restricted VF file from a font path and selected instance names.

	Creates the output directory if it does not exist.
	Raises ValueError for invalid inputs, or propagates fontTools errors.
	"""
	font = TTFont(font_path)
	hull = compute_hull(font, selected_names)
	if not hull:
		raise ValueError('No valid instances selected')
	# Warn if any axis default falls outside the restricted range — fonttools silently clamps it.
	fvar = font['fvar']
	for ax in fvar.axes:
		constraint = hull.get(ax.axisTag)
		if isinstance(constraint, tuple):
			lo, hi = constraint
			if not (lo <= ax.defaultValue <= hi):
				clamped = max(lo, min(hi, ax.defaultValue))
				print(
					f'Warning: {ax.axisTag} default ({ax.defaultValue}) is outside '
					f'restricted range [{lo}, {hi}]. Default will be clamped to {clamped}.'
				)
	# Ensure the output directory exists before writing.
	output_dir = os.path.dirname(output_path)
	if output_dir:
		os.makedirs(output_dir, exist_ok=True)
	partial = instancer.instantiateVariableFont(font, hull)
	patch_name_table(partial, family_name)
	partial.save(output_path)


# ---------------------------------------------------------------------------
# Output extension map
# ---------------------------------------------------------------------------

# Maps format label to file extension.
FORMAT_EXTENSIONS = {
	'TTF': '.ttf',
	'OTF': '.otf',
	'WOFF': '.woff',
	'WOFF2': '.woff2',
}

# Ordered list for indexing into the popup.
FORMAT_LABELS = list(FORMAT_EXTENSIONS.keys())


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class VFClampController:
	"""Floating window controller for generating restricted variable fonts."""

	# Window dimensions
	WINDOW_WIDTH = 500
	WINDOW_HEIGHT = 340

	def __init__(self):
		"""Initialise the controller window and all UI elements."""
		self._font_path = None
		self._instance_names = []
		self._output_folder = None

		w = self.WINDOW_WIDTH
		h = self.WINDOW_HEIGHT

		self.w = vanilla.FloatingWindow(
			(w, h),
			'vf-clamp — Generate Restricted VFs',
			minSize=(w, h),
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
			(16, 72, -16, 17),
			'Named Instances (select one or more)',
		)
		self.w.instanceList = vanilla.List(
			(16, 92, -16, 110),
			[],
			allowsMultipleSelection=True,
			selectionCallback=self._on_selection_change,
		)

		# -- Output name section ------------------------------------------------
		self.w.outputNameLabel = vanilla.TextBox(
			(16, 214, -16, 17),
			'Output Family Name',
		)
		self.w.outputNameField = vanilla.EditText(
			(16, 234, -16, 22),
			placeholder='Auto-generated from selection…',
		)

		# -- Format section -----------------------------------------------------
		self.w.formatLabel = vanilla.TextBox(
			(16, 270, 60, 17),
			'Format',
		)
		self.w.formatPopUp = vanilla.PopUpButton(
			(16, 288, 100, 22),
			FORMAT_LABELS,
		)

		# -- Output folder section ----------------------------------------------
		self.w.outputFolderLabel = vanilla.TextBox(
			(130, 270, -16, 17),
			'Output Folder',
		)
		self.w.outputFolderField = vanilla.EditText(
			(130, 288, -110, 22),
			placeholder='Same folder as font…',
			readOnly=True,
		)
		self.w.chooseFolderButton = vanilla.Button(
			(-102, 288, 86, 22),
			'Choose…',
			callback=self._on_choose_folder,
		)

		# -- Generate / status --------------------------------------------------
		self.w.generateButton = vanilla.Button(
			(16, -40, 120, 22),
			'Generate',
			callback=self._on_generate,
		)
		# Disable until a font is loaded and at least one instance is selected.
		self.w.generateButton.enable(False)

		self.w.statusLabel = vanilla.TextBox(
			(150, -37, -16, 17),
			'',
		)

		self.w.open()

	# -------------------------------------------------------------------------
	# Internal helpers
	# -------------------------------------------------------------------------

	def _update_generate_button(self):
		"""Enable the Generate button only when a font and at least one instance are ready."""
		indices = self.w.instanceList.getSelection()
		enabled = bool(self._font_path and indices)
		self.w.generateButton.enable(enabled)

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
			self.w.statusLabel.set('Could not read selected file path.')
			return

		path = str(url.path())
		self._font_path = path
		self.w.fontPathField.set(path)

		# Default output folder to the font's containing directory.
		self._output_folder = os.path.dirname(path)
		self.w.outputFolderField.set(self._output_folder)

		self._load_instances(path)

	def _load_instances(self, path):
		"""Parse fvar named instances from the font and populate the list."""
		self._instance_names = []
		self.w.instanceList.set([])
		self.w.outputNameField.set('')
		self.w.statusLabel.set('')
		self.w.generateButton.enable(False)

		try:
			font = TTFont(path)
		except Exception as exc:
			self.w.statusLabel.set(f'Error loading font: {exc}')
			print(f'vf-clamp: error loading font at {path!r}: {exc}')
			return

		if 'fvar' not in font:
			self.w.statusLabel.set('Not a variable font — no fvar table found.')
			return

		fvar = font['fvar']
		name_table = font['name']
		names = []
		for inst in fvar.instances:
			label = name_table.getDebugName(inst.subfamilyNameID)
			if label:
				names.append(label)

		self._instance_names = names
		self.w.instanceList.set(names)

		if names:
			self.w.statusLabel.set(f'{len(names)} named instance(s) found.')
		else:
			self.w.statusLabel.set('No named instances found in this font.')

	def _on_selection_change(self, sender):
		"""Update the output name field and Generate button when the selection changes."""
		indices = self.w.instanceList.getSelection()
		if not indices:
			self.w.outputNameField.set('')
			self._update_generate_button()
			return

		selected = [self._instance_names[i] for i in indices]
		name = compact_name(selected[0], selected[-1])
		self.w.outputNameField.set(name)
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
			self.w.statusLabel.set('Could not read selected folder path.')
			return

		self._output_folder = str(url.path())
		self.w.outputFolderField.set(self._output_folder)

	def _on_generate(self, sender):
		"""Validate inputs and call produce_restricted_vf to write the output file."""
		self.w.statusLabel.set('Processing…')

		try:
			# -- Validate ----------------------------------------------------------
			if not self._font_path:
				self.w.statusLabel.set('No font selected.')
				return

			indices = self.w.instanceList.getSelection()
			if not indices:
				self.w.statusLabel.set('Select at least one instance.')
				return

			family_name = self.w.outputNameField.get().strip()
			if not family_name:
				self.w.statusLabel.set('Output family name is required.')
				return

			output_folder = self._output_folder or os.path.dirname(self._font_path)

			# Use the selected index to look up the format label, then the extension.
			format_index = self.w.formatPopUp.get()
			format_label = FORMAT_LABELS[format_index]
			ext = FORMAT_EXTENSIONS.get(format_label, '.ttf')

			# Sanitize family name for use as a filename component.
			safe_name = sanitize_filename(family_name)
			output_path = os.path.join(output_folder, f'{safe_name}{ext}')

			selected_names = [self._instance_names[i] for i in indices]

			# -- Generate ----------------------------------------------------------
			produce_restricted_vf(
				self._font_path,
				selected_names,
				family_name,
				output_path,
			)
			self.w.statusLabel.set(f'Saved → {os.path.basename(output_path)}')

		except (ValueError, AssertionError) as exc:
			# Validation errors and fontTools assertion failures.
			self.w.statusLabel.set(f'Error: {exc}')
			print(f'vf-clamp: generation error: {exc}')
		except Exception as exc:
			# Unexpected errors — show clean message, log full detail to console.
			self.w.statusLabel.set(f'Error: {exc}')
			import traceback
			print(f'vf-clamp: unexpected error during generation:')
			traceback.print_exc()
