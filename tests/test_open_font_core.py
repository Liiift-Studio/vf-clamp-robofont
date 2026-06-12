# Basic tests for the new open_font_core helpers (issue #55 partial).
# These tests deliberately stay away from the VFClampController (mocking vanilla
# + RoboFont's AppKit surface is impractical without a real RoboFont host) and
# focus on the pure helpers split into open_font_core.py.

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Load open_font_core directly from its file so we avoid evaluating
# vfClamp/__init__.py (which pulls in vanilla — unavailable on CI Python).
PLUGIN_LIB = Path(__file__).resolve().parent.parent / 'vf-clamp.roboFontExt' / 'lib'
OPEN_FONT_CORE_PATH = PLUGIN_LIB / 'vfClamp' / 'open_font_core.py'

_spec = importlib.util.spec_from_file_location('open_font_core', OPEN_FONT_CORE_PATH)
open_font_core = importlib.util.module_from_spec(_spec)
sys.modules['open_font_core'] = open_font_core
_spec.loader.exec_module(open_font_core)


class TestComputeOpenFontHull(unittest.TestCase):
	"""compute_open_font_hull should match the controller's designspace-side hull math."""

	def test_empty_inputs_return_empty_hull(self):
		"""No locations and no indices means no hull — never raises."""
		self.assertEqual(open_font_core.compute_open_font_hull([], []), {})
		self.assertEqual(open_font_core.compute_open_font_hull(None, [0]), {})

	def test_single_instance_pins_every_axis(self):
		"""Selecting a single instance pins every axis at its location value."""
		locations = [{'wght': 400, 'wdth': 100}]
		hull = open_font_core.compute_open_font_hull(locations, [0])
		self.assertEqual(hull, {'wght': (400, 400), 'wdth': (100, 100)})

	def test_two_instances_expand_axis(self):
		"""Multi-instance selection widens the hull along varying axes."""
		locations = [
			{'wght': 300, 'wdth': 100},
			{'wght': 700, 'wdth': 100},
		]
		hull = open_font_core.compute_open_font_hull(locations, [0, 1])
		self.assertEqual(hull['wght'], (300, 700))
		# wdth shared between both instances — stays pinned.
		self.assertEqual(hull['wdth'], (100, 100))

	def test_out_of_range_indices_are_skipped(self):
		"""Stale indices left over after a list refresh do not raise."""
		locations = [{'wght': 400}]
		hull = open_font_core.compute_open_font_hull(locations, [0, 5, -1])
		self.assertEqual(hull, {'wght': (400, 400)})

	def test_partial_axis_overlap(self):
		"""Instances may carry different axes; hull keeps the union."""
		locations = [
			{'wght': 400, 'wdth': 100},
			{'wght': 600},
		]
		hull = open_font_core.compute_open_font_hull(locations, [0, 1])
		self.assertEqual(hull['wght'], (400, 600))
		# wdth only present on first instance — stays at that value.
		self.assertEqual(hull['wdth'], (100, 100))


class TestFrontmostOpenFont(unittest.TestCase):
	"""frontmost_open_font returns None outside RoboFont and never raises."""

	def test_returns_none_when_robofont_missing(self):
		"""Outside RoboFont mojo.roboFont is unimportable so the helper returns None."""
		# Outside a RoboFont host the import flag is False — we just assert the
		# helper doesn't raise and returns None.
		result = open_font_core.frontmost_open_font()
		self.assertIsNone(result)


class TestListOpenFonts(unittest.TestCase):
	"""list_open_fonts returns [] outside RoboFont and never raises."""

	def test_returns_empty_list_when_robofont_missing(self):
		"""Outside a RoboFont host the helper returns an empty list instead of raising."""
		self.assertEqual(open_font_core.list_open_fonts(), [])


class TestIsDesignspacePath(unittest.TestCase):
	"""is_designspace_path validates extension AND existence."""

	def test_empty_returns_false(self):
		"""None / '' / 0 short-circuit to False."""
		self.assertFalse(open_font_core.is_designspace_path(None))
		self.assertFalse(open_font_core.is_designspace_path(''))

	def test_nonexistent_path_returns_false(self):
		"""A would-be designspace path that doesn't exist on disk is rejected."""
		self.assertFalse(open_font_core.is_designspace_path('/tmp/definitely-not-here.designspace'))

	def test_wrong_extension_returns_false(self):
		"""A real file with the wrong extension is rejected even if it exists."""
		with tempfile.NamedTemporaryFile(suffix='.ufo', delete=False) as tmp:
			tmp.write(b'fake')
			tmp_name = tmp.name
		try:
			self.assertFalse(open_font_core.is_designspace_path(tmp_name))
		finally:
			os.unlink(tmp_name)

	def test_real_designspace_returns_true(self):
		"""A real .designspace file passes."""
		with tempfile.NamedTemporaryFile(suffix='.designspace', delete=False) as tmp:
			tmp.write(b'<designspace/>')
			tmp_name = tmp.name
		try:
			self.assertTrue(open_font_core.is_designspace_path(tmp_name))
		finally:
			os.unlink(tmp_name)


if __name__ == '__main__':
	unittest.main()
