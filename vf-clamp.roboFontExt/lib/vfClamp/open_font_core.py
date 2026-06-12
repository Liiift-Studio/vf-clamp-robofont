# open_font_core.py — RoboFont open-font source subsystem for vf-clamp.
# Operates on `defcon` / `fontParts` `Font` objects pulled from `mojo.roboFont`
# and the matching `.designspace` files; depends on the RoboFont Python runtime
# when active, and degrades to no-op when imported outside RoboFont (e.g. CI).
#
# Split from controller.py (issue #47) so the fontTools/binary subsystem in
# controller.py stays distinct from the defcon/fontParts/ufo2ft subsystem here.
#
# Important design constraint: a UFO is a single master, not a variable font.
# "Clamping" a UFO does not make sense in vf-clamp's domain — the only way to
# produce a restricted VF from open RoboFont sources is to detect a sibling
# .designspace, compile it to a TTFont via ufo2ft.compileVariableTTF, and feed
# the result through the existing produce_restricted_vf pipeline. The output is
# a .ttf binary; we never write back to a UFO.

import os

# Capability flags. Both gates are independent so we can report 'RoboFont is
# present but ufo2ft is not' or vice versa, instead of a single AVAILABLE bit.
try:
	from mojo.roboFont import AllFonts, CurrentFont
	_ROBOFONT_AVAILABLE = True
	_ROBOFONT_IMPORT_ERROR = None
except ImportError as _err:
	_ROBOFONT_AVAILABLE = False
	_ROBOFONT_IMPORT_ERROR = str(_err)
	AllFonts = None  # type: ignore
	CurrentFont = None  # type: ignore

try:
	from fontTools.designspaceLib import DesignSpaceDocument
	_DESIGNSPACELIB_AVAILABLE = True
except ImportError:
	_DESIGNSPACELIB_AVAILABLE = False
	DesignSpaceDocument = None  # type: ignore

try:
	from ufo2ft import compileVariableTTF
	_UFO2FT_AVAILABLE = True
	_UFO2FT_IMPORT_ERROR = None
except ImportError as _err:
	_UFO2FT_AVAILABLE = False
	_UFO2FT_IMPORT_ERROR = str(_err)
	compileVariableTTF = None  # type: ignore


def is_robofont_available():
	"""Return True when running inside RoboFont with the mojo.roboFont API."""
	return _ROBOFONT_AVAILABLE


def is_ufo2ft_available():
	"""Return True when ufo2ft is importable (needed to compile a designspace to VF)."""
	return _UFO2FT_AVAILABLE


def robofont_import_error():
	"""Return the mojo.roboFont import-error string, or None when importable."""
	return _ROBOFONT_IMPORT_ERROR


def ufo2ft_import_error():
	"""Return the ufo2ft import-error string, or None when importable."""
	return _UFO2FT_IMPORT_ERROR


def list_open_fonts():
	"""Return the list of currently-open `RFont` objects, or [] when RoboFont is absent."""
	if not _ROBOFONT_AVAILABLE:
		return []
	try:
		return list(AllFonts())
	except Exception:
		return []


def open_font_label(font):
	"""Return a short, human-readable label for an open RFont (used in PopUp lists).

	Prefers familyName from font.info; falls back to the basename of the UFO
	path; finally 'Untitled'. Suffixes the UFO basename in parentheses when
	available so users with multiple variants of the same family can tell them
	apart at a glance.
	"""
	family = ''
	try:
		family = (font.info.familyName or '') if getattr(font, 'info', None) else ''
	except (AttributeError, RuntimeError):
		family = ''
	path = ''
	try:
		path = getattr(font, 'path', '') or ''
	except (AttributeError, RuntimeError):
		path = ''
	base = os.path.basename(path) if path else ''
	if family and base:
		return f'{family}  ({base})'
	return family or base or 'Untitled'


def find_sibling_designspace(font):
	"""Return the absolute path to a sibling `.designspace` for ``font``, or None.

	Strategy: look in the UFO's parent directory for any `.designspace` file
	that mentions the UFO's basename in a `<source filename=...>` element.
	Falls back to 'any .designspace in the same folder' when no name-match
	hits but exactly one designspace exists there.
	"""
	if not _DESIGNSPACELIB_AVAILABLE:
		return None
	try:
		ufo_path = getattr(font, 'path', None) or ''
	except (AttributeError, RuntimeError):
		ufo_path = ''
	if not ufo_path:
		return None
	folder = os.path.dirname(ufo_path)
	if not folder or not os.path.isdir(folder):
		return None
	ufo_base = os.path.basename(ufo_path)
	candidates = []
	try:
		for entry in os.listdir(folder):
			if entry.lower().endswith('.designspace'):
				candidates.append(os.path.join(folder, entry))
	except OSError:
		return None
	if not candidates:
		return None
	# Prefer a designspace whose <source> element references this UFO.
	for ds_path in candidates:
		try:
			doc = DesignSpaceDocument.fromfile(ds_path)
		except Exception:
			continue
		for src in doc.sources:
			src_filename = os.path.basename(getattr(src, 'filename', '') or '')
			if src_filename == ufo_base:
				return ds_path
	# Fallback: exactly one designspace in the folder — assume it's the right one.
	if len(candidates) == 1:
		return candidates[0]
	return None


def compile_designspace_to_ttfont(designspace_path):
	"""Compile ``designspace_path`` to an in-memory ``TTFont``.

	Raises ``RuntimeError`` if ufo2ft is unavailable or compilation fails. The
	caller hands the result straight to ``produce_restricted_vf``-style code.
	"""
	if not _UFO2FT_AVAILABLE:
		raise RuntimeError(
			f'ufo2ft is required to compile a designspace to a variable font: '
			f'{_UFO2FT_IMPORT_ERROR}'
		)
	if not _DESIGNSPACELIB_AVAILABLE or DesignSpaceDocument is None:
		raise RuntimeError(
			'fontTools.designspaceLib is required to load .designspace files.'
		)
	try:
		doc = DesignSpaceDocument.fromfile(designspace_path)
	except Exception as e:
		raise RuntimeError(f'Could not parse designspace: {e}') from e
	try:
		ttfont = compileVariableTTF(doc)
	except Exception as e:
		raise RuntimeError(f'Failed to compile variable font: {e}') from e
	return ttfont


def get_instance_names_from_designspace(designspace_path):
	"""Return ordered instance names from a `.designspace` file.

	Useful before a full compile — the user can see what they're about to clamp
	without paying the (slow) compileVariableTTF cost.
	"""
	if not _DESIGNSPACELIB_AVAILABLE or DesignSpaceDocument is None:
		return []
	try:
		doc = DesignSpaceDocument.fromfile(designspace_path)
	except Exception:
		return []
	names = []
	seen = {}
	for inst in doc.instances:
		# DesignSpaceDocument.instances expose styleName / familyName / name.
		# Prefer styleName because that's what fvar's subfamilyName ends up
		# carrying after compileVariableTTF.
		label = (getattr(inst, 'styleName', '') or getattr(inst, 'name', '') or '').strip()
		if not label:
			continue
		if label in seen:
			seen[label] += 1
			names.append(f'{label} #{seen[label]}')
		else:
			seen[label] = 1
			names.append(label)
	return names


def frontmost_open_font():
	"""Return the frontmost currently-open RFont, or None when RoboFont is absent."""
	if not _ROBOFONT_AVAILABLE or CurrentFont is None:
		return None
	try:
		return CurrentFont()
	except Exception:
		return None
