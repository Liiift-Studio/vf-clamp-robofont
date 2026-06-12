# formats.py — central registry of output formats supported by vf-clamp (RoboFont).
# Every site that needs to dispatch on a format label imports from here so
# adding a new format only requires editing this one file. Mirrors the Glyphs
# plugin's formats.py so behaviour stays consistent across the suite.

# Sentinel mapping. Each entry pins down everything the rest of the code needs
# to know about a format: the file extension, the fontTools flavor string for
# WOFF/WOFF2 wrappers, and whether the format inherits its extension from the
# source SFNT type (TTF or OTF).

_FORMATS = {
	'TTF/OTF (original)': {
		'extension': '',          # '' = inherit from source (.ttf or .otf)
		'flavor': None,
		'inherits_ext': True,
	},
	'TTF': {
		'extension': '.ttf',
		'flavor': None,
		'inherits_ext': False,
	},
	'OTF': {
		'extension': '.otf',
		'flavor': None,
		'inherits_ext': False,
	},
	'WOFF': {
		'extension': '.woff',
		'flavor': 'woff',
		'inherits_ext': False,
	},
	'WOFF2': {
		'extension': '.woff2',
		'flavor': 'woff2',
		'inherits_ext': False,
	},
}


def _entry(fmt):
	"""Return the registry entry for ``fmt``. None if unknown.

	Performs an exact-key lookup so display labels survive (matters for the
	'TTF/OTF (original)' entry whose label is user-facing).
	"""
	if fmt is None:
		return None
	return _FORMATS.get(fmt)


def known_formats():
	"""Return a tuple of every registered format label, preserving insertion order."""
	return tuple(_FORMATS.keys())


def is_known_format(fmt):
	"""Return True when ``fmt`` is a registered format label."""
	return _entry(fmt) is not None


def extension_for(fmt):
	"""Return the file extension for ``fmt``. '' means 'inherit from source'."""
	entry = _entry(fmt)
	return entry['extension'] if entry else ''


def flavor_for(fmt):
	"""Return the fontTools flavor string for ``fmt`` (or None for raw sfnt)."""
	entry = _entry(fmt)
	return entry['flavor'] if entry else None


def inherits_ext(fmt):
	"""Return True when this format's extension is derived from the source file."""
	entry = _entry(fmt)
	return bool(entry and entry['inherits_ext'])
