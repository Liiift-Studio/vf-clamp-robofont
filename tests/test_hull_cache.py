# Regression tests for the hull-preview memoization added in #44.
# The hull preview used to recompute compute_hull() on every selection change
# (and several times per change). These tests verify the cache wrapper on
# VFClampController._hull_for_preview: same selection computes once, source
# changes invalidate, and the cached result matches a direct compute.
#
# Like test_open_font_core, we avoid evaluating vfClamp/__init__.py (which
# imports vanilla — unavailable on CI Python). We load controller.py directly
# with a stubbed `vanilla` module; its AppKit imports are already guarded so
# they degrade gracefully off a RoboFont host.

import importlib.util
import sys
import types
import unittest
from pathlib import Path

PLUGIN_LIB = Path(__file__).resolve().parent.parent / 'vf-clamp.roboFontExt' / 'lib'

# Stub the unconditional `import vanilla` before loading the controller.
sys.modules.setdefault('vanilla', types.ModuleType('vanilla'))

# Provide a `vfClamp` package object so controller's `from vfClamp import ...`
# (e.g. open_font_core) resolves without running __init__.py.
if 'vfClamp' not in sys.modules:
	_pkg = types.ModuleType('vfClamp')
	_pkg.__path__ = [str(PLUGIN_LIB / 'vfClamp')]
	sys.modules['vfClamp'] = _pkg

_CTRL_PATH = PLUGIN_LIB / 'vfClamp' / 'controller.py'
_spec = importlib.util.spec_from_file_location('vfClamp.controller', _CTRL_PATH)
controller = importlib.util.module_from_spec(_spec)
sys.modules['vfClamp.controller'] = controller
_spec.loader.exec_module(controller)


def _make_variable_font(instance_coords):
	"""Build a minimal in-memory TTFont with an fvar table.

	``instance_coords`` is a list of {tag: value} dicts, one per named
	instance. Only the fvar table is populated — that is all compute_hull
	reads — so we avoid shipping a binary fixture into the submodule.
	"""
	from fontTools.ttLib import TTFont, newTable
	from fontTools.ttLib.tables._f_v_a_r import Axis, NamedInstance

	tags = sorted({t for coords in instance_coords for t in coords})
	font = TTFont()
	fvar = newTable('fvar')
	for tag in tags:
		axis = Axis()
		axis.axisTag = tag
		axis.minValue = min(c.get(tag, 0) for c in instance_coords)
		axis.maxValue = max(c.get(tag, 0) for c in instance_coords)
		axis.defaultValue = axis.minValue
		fvar.axes.append(axis)
	for coords in instance_coords:
		inst = NamedInstance()
		inst.coordinates = dict(coords)
		fvar.instances.append(inst)
	font['fvar'] = fvar
	return font


class _StubController:
	"""Minimal stand-in exposing only the state the cache methods read.

	Binds the real VFClampController cache methods so we exercise the shipped
	code, but counts compute invocations via an instrumented
	_compute_hull_for_preview override.
	"""

	SOURCE_FILE = controller.VFClampController.SOURCE_FILE
	SOURCE_OPEN_FONT = controller.VFClampController.SOURCE_OPEN_FONT

	def __init__(self, font):
		self._source_mode = self.SOURCE_FILE
		self._font = font
		self._designspace_path = None
		self._instance_names = []
		self._hull_cache = {}
		self._hull_cache_source_key = None
		self._hull_cache_source_ref = None
		self.compute_calls = 0

	_hull_source_key = controller.VFClampController._hull_source_key
	_hull_for_preview = controller.VFClampController._hull_for_preview

	def _compute_hull_for_preview(self, valid_indices):
		self.compute_calls += 1
		return controller.VFClampController._compute_hull_for_preview(
			self, valid_indices,
		)


class TestHullCache(unittest.TestCase):
	"""Memoization of the hull preview keyed on (source, selected indices)."""

	def setUp(self):
		self.coords = [
			{'wght': 100, 'opsz': 14},
			{'wght': 400, 'opsz': 14},
			{'wght': 900, 'opsz': 14},
		]
		self.font = _make_variable_font(self.coords)
		self.stub = _StubController(self.font)

	def test_cached_hull_matches_direct_compute(self):
		"""The cached result equals an uncached compute for the same indices."""
		idx = [0, 2]
		direct = controller.VFClampController._compute_hull_for_preview(
			self.stub, idx,
		)
		self.stub.compute_calls = 0
		cached = self.stub._hull_for_preview(idx)
		self.assertEqual(cached, direct)
		self.assertEqual(cached, {'opsz': (14, 14), 'wght': (100, 900)})

	def test_repeat_and_permutation_hit_cache(self):
		"""Re-asking for the same selection (any order) computes only once."""
		idx = [0, 2]
		self.stub._hull_for_preview(idx)
		self.stub._hull_for_preview(idx)
		self.stub._hull_for_preview([2, 0])  # permutation of the same selection
		self.assertEqual(self.stub.compute_calls, 1)

	def test_distinct_selection_recomputes(self):
		"""A different selection is a cache miss and computes again."""
		self.stub._hull_for_preview([0, 2])
		self.stub._hull_for_preview([0, 1, 2])
		self.assertEqual(self.stub.compute_calls, 2)

	def test_source_change_invalidates_cache(self):
		"""Swapping the loaded font drops the cache so nothing stale leaks."""
		idx = [0, 2]
		first = self.stub._hull_for_preview(idx)
		# Replace with a font whose same indices yield a DIFFERENT hull.
		self.stub._font = _make_variable_font([
			{'wght': 200, 'opsz': 14},
			{'wght': 400, 'opsz': 14},
			{'wght': 700, 'opsz': 14},
		])
		self.stub.compute_calls = 0
		second = self.stub._hull_for_preview(idx)
		self.assertEqual(self.stub.compute_calls, 1)  # recomputed, not served stale
		self.assertNotEqual(second, first)
		self.assertEqual(second, {'opsz': (14, 14), 'wght': (200, 700)})

	def test_empty_selection_short_circuits(self):
		"""An empty selection returns {} without touching the cache or compute."""
		self.assertEqual(self.stub._hull_for_preview([]), {})
		self.assertEqual(self.stub.compute_calls, 0)
		self.assertEqual(self.stub._hull_cache, {})


if __name__ == '__main__':
	unittest.main()
