# vf-clamp controller — vanilla UI for generating restricted variable fonts from named instance ranges.

import os
import re

import vanilla
from AppKit import NSOpenPanel, NSFileHandlingPanelOKButton
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def compute_hull(font, selected_names):
	"""Compute axis hull (min/max per axis) across selected named instances."""
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
		result[tag] = lo if lo == hi else instancer.AxisRange(lo, hi)
	return result


def patch_name_table(font, family_name):
	"""Update name table so restricted VF reflects its instance range."""
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
			record.string = value.encode('utf-16-be')
		elif record.platformID == 1:
			try:
				record.string = value.encode('mac_roman')
			except Exception:
				record.string = value.encode('ascii', errors='replace')


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


def produce_restricted_vf(font_path, selected_names, family_name, output_path):
	"""Produce one restricted VF file from a font path and selected instance names."""
	font = TTFont(font_path)
	hull = compute_hull(font, selected_names)
	if not hull:
		raise ValueError('No valid instances selected')
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
			list(FORMAT_EXTENSIONS.keys()),
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
		self.w.statusLabel = vanilla.TextBox(
			(150, -37, -16, 17),
			'',
		)

		self.w.open()

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
		if result != NSFileHandlingPanelOKButton:
			return

		path = str(panel.URL().path())
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

		try:
			font = TTFont(path)
		except Exception as exc:
			self.w.statusLabel.set(f'Error loading font: {exc}')
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
		"""Update the output name field when the instance selection changes."""
		indices = self.w.instanceList.getSelection()
		if not indices:
			self.w.outputNameField.set('')
			return

		selected = [self._instance_names[i] for i in indices]
		name = compact_name(selected[0], selected[-1])
		self.w.outputNameField.set(name)

	def _on_choose_folder(self, sender):
		"""Open a folder picker and store the chosen output directory."""
		panel = NSOpenPanel.openPanel()
		panel.setCanChooseFiles_(False)
		panel.setCanChooseDirectories_(True)
		result = panel.runModal()
		if result != NSFileHandlingPanelOKButton:
			return

		self._output_folder = str(panel.URL().path())
		self.w.outputFolderField.set(self._output_folder)

	def _on_generate(self, sender):
		"""Validate inputs and call produce_restricted_vf to write the output file."""
		self.w.statusLabel.set('Working…')

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

		format_label = self.w.formatPopUp.getItem()
		ext = FORMAT_EXTENSIONS.get(format_label, '.ttf')

		safe_name = re.sub(r'[^A-Za-z0-9_\-]', '_', family_name)
		output_path = os.path.join(output_folder, f'{safe_name}{ext}')

		selected_names = [self._instance_names[i] for i in indices]

		# -- Generate ----------------------------------------------------------
		try:
			produce_restricted_vf(
				self._font_path,
				selected_names,
				family_name,
				output_path,
			)
			self.w.statusLabel.set(f'Saved → {os.path.basename(output_path)}')
		except Exception as exc:
			self.w.statusLabel.set(f'Error: {exc}')
