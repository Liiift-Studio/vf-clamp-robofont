#!/usr/bin/env python3
# render_dialog.py — full-dialog snapshot harness for the RoboFont extension.
#
# Sibling to render_views.py (which renders just the two custom NSViews).
# This one paints the *entire* FloatingWindow chrome — source picker,
# instance list, filter + select chips, preset popup, output zone, log pane,
# action bar — mirroring vf-clamp.roboFontExt v1.2.0 dimensions (620 × 834).
# Real HullPlotView and AnimatedPreviewView are mounted as subviews so the
# only mock content is the chrome.
#
# Adapts the structure of the Glyphs plugin's render_dialog.py for RoboFont's
# different widget layout (single-column with right-anchored bulk-select
# buttons rather than two-column dashboard).

import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.normpath(os.path.join(
	THIS_DIR, '..', 'vf-clamp.roboFontExt', 'lib', 'vfClamp',
))
sys.path.insert(0, PKG_DIR)

from AppKit import (  # noqa: E402
	NSApplication, NSBitmapImageRep, NSBitmapImageFileTypePNG,
	NSGraphicsContext, NSColor, NSRectFill, NSWindow, NSBackingStoreBuffered,
	NSImage, NSCompositingOperationSourceOver, NSAppearance,
	NSBezierPath, NSFont, NSAttributedString, NSMutableParagraphStyle,
	NSTextAlignmentCenter, NSTextAlignmentLeft, NSTextAlignmentRight,
	NSForegroundColorAttributeName, NSFontAttributeName,
	NSParagraphStyleAttributeName, NSView,
)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize  # noqa: E402

NSApplication.sharedApplication()

from hull_plot import make_hull_plot_view  # noqa: E402
from preview_view import make_preview_view, ANIM_PERIOD  # noqa: E402
from render_views import synthetic_fixture, hull_from, WEIGHTS, SIZES  # noqa: E402


# RoboFont controller dimensions — match WINDOW_WIDTH/WINDOW_HEIGHT and the
# layout constants in vf-clamp.roboFontExt/lib/vfClamp/controller.py.
# Mock grows the height slightly beyond the production 834 because the mock
# adds explicit padding that the real layout absorbs through margin
# collapse (vanilla.* widgets sometimes draw slightly outside their declared
# bounds). 920 ensures the action bar lands inside the view.
W = 620
H = 920
PAD = 16
LABEL_COL_W = 110
LABEL_GAP = 12
CONTROL_X = PAD + LABEL_COL_W + LABEL_GAP  # 138
ROW = 28
LABEL_H = 20
FIELD_H = 22
BTN_H = 24
PLOT_H = 140
SPECIMEN_H = 60
LOG_H = 84


# ---------------------------------------------------------------------------
# Drawing helpers — same shape language the glyphs render_dialog uses so
# the two mocks read as siblings.
# ---------------------------------------------------------------------------

def _para(align):
	p = NSMutableParagraphStyle.alloc().init()
	p.setAlignment_(align)
	return p


def _text(s, point, size=12.0, color=None, align=NSTextAlignmentLeft, bold=False):
	if color is None:
		color = NSColor.labelColor()
	font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
	attrs = {
		NSFontAttributeName: font,
		NSForegroundColorAttributeName: color,
		NSParagraphStyleAttributeName: _para(align),
	}
	NSAttributedString.alloc().initWithString_attributes_(s, attrs).drawAtPoint_(point)


def _text_in_rect(s, rect, size=12.0, color=None, align=NSTextAlignmentLeft, bold=False):
	if color is None:
		color = NSColor.labelColor()
	font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
	attrs = {
		NSFontAttributeName: font,
		NSForegroundColorAttributeName: color,
		NSParagraphStyleAttributeName: _para(align),
	}
	att = NSAttributedString.alloc().initWithString_attributes_(s, attrs)
	tsize = att.size()
	y = rect.origin.y + (rect.size.height - tsize.height) / 2.0
	if align == NSTextAlignmentCenter:
		x = rect.origin.x + (rect.size.width - tsize.width) / 2.0
	elif align == NSTextAlignmentRight:
		x = rect.origin.x + rect.size.width - tsize.width - 4
	else:
		x = rect.origin.x + 4
	att.drawAtPoint_(NSMakePoint(x, y))


def _zone_header(text, x, y, w):
	NSAttributedString.alloc().initWithString_attributes_(
		text,
		{
			NSFontAttributeName: NSFont.systemFontOfSize_(9.5),
			NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
		},
	).drawAtPoint_(NSMakePoint(x, y))


def _right_label(text, x, y, w):
	font = NSFont.systemFontOfSize_(12.0)
	attrs = {
		NSFontAttributeName: font,
		NSForegroundColorAttributeName: NSColor.labelColor(),
		NSParagraphStyleAttributeName: _para(NSTextAlignmentRight),
	}
	NSAttributedString.alloc().initWithString_attributes_(text, attrs).drawAtPoint_(
		NSMakePoint(x, y),
	)


def _radio(x, y, size, checked, label):
	r = NSMakeRect(x, y, size, size)
	NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).set()
	NSBezierPath.bezierPathWithOvalInRect_(r).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.30).set()
	b = NSBezierPath.bezierPathWithOvalInRect_(r)
	b.setLineWidth_(1.0)
	b.stroke()
	if checked:
		inner = NSMakeRect(x + 3, y + 3, size - 6, size - 6)
		NSColor.controlAccentColor().set()
		NSBezierPath.bezierPathWithOvalInRect_(inner).fill()
	_text_in_rect(label, NSMakeRect(x + size + 6, y - 2, 200, size + 4))


def _popup(x, y, w, h, text):
	r = NSMakeRect(x, y, w, h)
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 4.0, 4.0).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).set()
	b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 4.0, 4.0)
	b.setLineWidth_(1.0)
	b.stroke()
	_text_in_rect(text, NSMakeRect(x + 4, y, w - 22, h))
	cx = x + w - 12
	cy = y + h / 2.0
	chev = NSBezierPath.bezierPath()
	chev.moveToPoint_(NSMakePoint(cx - 4, cy + 2))
	chev.lineToPoint_(NSMakePoint(cx, cy - 2))
	chev.lineToPoint_(NSMakePoint(cx + 4, cy + 2))
	NSColor.tertiaryLabelColor().set()
	chev.setLineWidth_(1.5)
	chev.stroke()


def _field(x, y, w, h, text, placeholder=False):
	r = NSMakeRect(x, y, w, h)
	NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 3.0, 3.0).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.16).set()
	b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 3.0, 3.0)
	b.setLineWidth_(1.0)
	b.stroke()
	color = NSColor.tertiaryLabelColor() if placeholder else NSColor.labelColor()
	_text_in_rect(text, NSMakeRect(x + 4, y, w - 8, h), color=color)


def _button(x, y, w, h, text, primary=False, sizeStyle='regular'):
	r = NSMakeRect(x, y, w, h)
	size = 11.0 if sizeStyle == 'small' else 12.0
	if primary:
		NSColor.controlAccentColor().set()
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0).fill()
		_text_in_rect(
			text, r, size=size, color=NSColor.whiteColor(),
			align=NSTextAlignmentCenter, bold=True,
		)
	else:
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0).fill()
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).set()
		b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6.0, 6.0)
		b.setLineWidth_(1.0)
		b.stroke()
		_text_in_rect(text, r, size=size, align=NSTextAlignmentCenter)


def _checkbox(x, y, size, checked, label):
	r = NSMakeRect(x, y, size, size)
	NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).set()
	NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 2.5, 2.5).fill()
	NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.30).set()
	b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 2.5, 2.5)
	b.setLineWidth_(1.0)
	b.stroke()
	if checked:
		NSColor.controlAccentColor().set()
		inner = NSMakeRect(x + 2.5, y + 2.5, size - 5, size - 5)
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inner, 1.5, 1.5).fill()
		check = NSBezierPath.bezierPath()
		check.moveToPoint_(NSMakePoint(x + 4, y + size / 2))
		check.lineToPoint_(NSMakePoint(x + size / 2 - 1, y + 4.5))
		check.lineToPoint_(NSMakePoint(x + size - 3.5, y + size - 4))
		NSColor.whiteColor().set()
		check.setLineWidth_(1.6)
		check.stroke()
	if label:
		_text_in_rect(label, NSMakeRect(x + size + 6, y - 2, 400, size + 4))


# ---------------------------------------------------------------------------
# Custom NSView painting the FloatingWindow chrome.
# ---------------------------------------------------------------------------

class DialogMockView(NSView):

	def isFlipped(self):
		return True

	def setMockState_(self, state):
		self._state = state

	def drawRect_(self, rect):
		st = self._state
		bounds = self.bounds()

		# Window background.
		NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.14, 0.14, 1.0).set()
		NSRectFill(bounds)

		# Title bar — same vibe as the glyphs mock.
		NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.10, 0.10, 1.0).set()
		NSRectFill(NSMakeRect(0, 0, W, 28))
		_text_in_rect(
			'◇ vf-clamp — Generate Restricted VFs',
			NSMakeRect(0, 0, W, 28),
			size=12.5, color=NSColor.secondaryLabelColor(),
			align=NSTextAlignmentCenter,
		)
		for i, rgb in enumerate([
			(0.97, 0.36, 0.34), (0.99, 0.74, 0.18), (0.20, 0.78, 0.35),
		]):
			NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0).set()
			NSBezierPath.bezierPathWithOvalInRect_(
				NSMakeRect(10 + i * 18, 9, 11, 11),
			).fill()

		# === Layout walker — mirrors controller._build_window ===
		y = 28 + PAD  # 44

		# Zone 1: SOURCE header
		_zone_header('SOURCE', PAD, y, W - 2 * PAD)
		y += 14

		# Source row + file row
		_right_label('Source:', PAD, y + 4, LABEL_COL_W)
		_radio(CONTROL_X, y + 4, 14, st['source'] == 'open', 'Open Font')
		_radio(CONTROL_X + 90, y + 4, 14, st['source'] == 'file', 'File')
		y += ROW + 6

		_right_label('Font:', PAD, y + 4, LABEL_COL_W)
		_popup(CONTROL_X, y, W - CONTROL_X - PAD, FIELD_H + 2, st['font_label'])
		y += ROW + 8

		# Divider
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
		NSRectFill(NSMakeRect(PAD, y, W - 2 * PAD, 1))
		y += 12

		# Zone 2: DASHBOARD header
		_zone_header('DASHBOARD', PAD, y, W - 2 * PAD)
		y += 14

		# Instances label + filter + bulk-select chips
		_right_label('Instances:', PAD, y + 2, LABEL_COL_W)
		_field(CONTROL_X, y, 160, FIELD_H, '', placeholder=True)
		_button(W - PAD - 246, y, 56, BTN_H, 'All', sizeStyle='small')
		_button(W - PAD - 188, y, 56, BTN_H, 'None', sizeStyle='small')
		_button(W - PAD - 130, y, 64, BTN_H, 'Invert', sizeStyle='small')
		_popup(W - PAD - 64, y, 64, BTN_H, '▾ More')
		y += LABEL_H + 6

		# Instance list (rendered as a styled rect with a few sample rows)
		list_h = 140
		lr = NSMakeRect(CONTROL_X, y, W - CONTROL_X - PAD, list_h)
		NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.20).set()
		NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(lr, 4.0, 4.0).fill()
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.14).set()
		bp = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(lr, 4.0, 4.0)
		bp.setLineWidth_(1.0)
		bp.stroke()
		row_h = 18
		visible = int(list_h / row_h)
		focus_set = set(st.get('focus_indices', []))
		for i in range(min(visible, len(st['names']))):
			ry = y + 2 + i * row_h
			if i in focus_set:
				NSColor.controlAccentColor().colorWithAlphaComponent_(0.55).set()
				NSRectFill(NSMakeRect(lr.origin.x + 2, ry, lr.size.width - 4, row_h))
			_text(st['names'][i], NSMakePoint(CONTROL_X + 8, ry), size=11.0)
		y += list_h + 4

		# Selection count line
		n_sel = len(focus_set)
		_text(
			f'{n_sel} of {len(st["names"])} selected',
			NSMakePoint(CONTROL_X, y),
			size=11.0, color=NSColor.tertiaryLabelColor(),
		)
		y += 14 + 4

		# Design space label
		_right_label('Design space:', PAD, y + 2, LABEL_COL_W)
		# Plot + size estimate + specimen — actual NSViews mounted by render_dialog().
		plot_y_rel = y
		# Reserve the right-column height. Skip past plot + size + specimen.
		y += PLOT_H
		# Size estimate strip
		_text(
			st['size_estimate'],
			NSMakePoint(CONTROL_X, y + 4),
			size=11.0, color=NSColor.secondaryLabelColor(),
		)
		y += 16
		# Specimen
		y += 22  # gap to specimen top
		y += SPECIMEN_H + 8

		# Divider
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
		NSRectFill(NSMakeRect(PAD, y, W - 2 * PAD, 1))
		y += 12

		# Zone 3: OUTPUT header
		_zone_header('OUTPUT', PAD, y, W - 2 * PAD)
		y += 14

		# Preset row
		_right_label('Preset:', PAD, y + 4, LABEL_COL_W)
		_popup(CONTROL_X, y, 240, FIELD_H + 2, st['preset'])
		y += ROW + 4

		# Output Name
		_right_label('Output Name:', PAD, y + 4, LABEL_COL_W)
		_field(CONTROL_X, y, W - CONTROL_X - PAD, FIELD_H, st['output_name'])
		y += ROW + 4

		# Format
		_right_label('Format:', PAD, y + 4, LABEL_COL_W)
		_popup(CONTROL_X, y, 200, FIELD_H + 2, st['format'])
		y += ROW + 4

		# Folder
		_right_label('Folder:', PAD, y + 4, LABEL_COL_W)
		field_w = W - CONTROL_X - PAD - 110
		_field(CONTROL_X, y, field_w, FIELD_H, st['folder'], placeholder=True)
		_button(CONTROL_X + field_w + 8, y - 1, 86, BTN_H, 'Choose…', sizeStyle='small')
		y += ROW + 4

		# Open after generating checkbox
		_checkbox(CONTROL_X, y, 14, True, 'Open output after generating')
		y += 20 + 8

		# Divider
		NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
		NSRectFill(NSMakeRect(PAD, y, W - 2 * PAD, 1))
		y += 12

		# LOG pane
		_text(
			'LOG',
			NSMakePoint(PAD + 12, y),
			size=10.5, color=NSColor.tertiaryLabelColor(), bold=True,
		)
		log_rect = NSMakeRect(PAD, y + 18, W - 2 * PAD, LOG_H - 18)
		NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.18).set()
		NSRectFill(log_rect)
		# Activity stripe
		NSColor.controlAccentColor().colorWithAlphaComponent_(0.6).set()
		NSRectFill(NSMakeRect(PAD, y + 18, 3, LOG_H - 18))
		for i, line in enumerate(st['log'][-3:]):
			_text(
				line,
				NSMakePoint(PAD + 10, y + 22 + i * 14),
				size=11.0, color=NSColor.labelColor(),
			)
		y += LOG_H + 8

		# Action bar with shortcut chips on the left
		ab_y = y + 4
		left_chips = ['⌘A All', '⌘D None', '⌘I Invert', '⇥ Nav', '␣ Toggle', '⏎ Gen']
		cx = PAD
		for chip in left_chips:
			_text(
				chip, NSMakePoint(cx, ab_y),
				size=10.5, color=NSColor.secondaryLabelColor(),
			)
			cx += 60
		# Right-anchored Cancel + Generate
		gen_w = 110
		can_w = 80
		gap = 8
		_button(W - PAD - gen_w, y, gen_w, BTN_H, 'Generate', primary=True)
		_button(W - PAD - gen_w - gap - can_w, y, can_w, BTN_H, 'Cancel')


# ---------------------------------------------------------------------------
# Compose + render
# ---------------------------------------------------------------------------

def fake_state(selected_indices):
	instances, names = synthetic_fixture()
	checked = set(int(i) for i in selected_indices)
	hull = hull_from(instances, list(checked))
	selected = sorted(checked)
	visible_names = names[:12]
	focus_indices = [i for i in range(12) if i in checked]
	return {
		'source': 'open',
		'font_label': 'Daith Adv (Daith-Italic Adv2 v2.glyphspackage)',
		'instances': instances,
		'names': visible_names,
		'focus_indices': focus_indices,
		'preset': '(no preset)',
		'output_name': 'Daith Adv 12 Extralight-42 Light Italic',
		'format': '.glyphs',
		'folder': 'Default: same folder as source',
		'size_estimate': (
			f'{len(checked)} instances  ·  {1 + len(checked) // 4} masters'
			f'  ·  {len(hull)} ax'
		),
		'log': [
			'Ready. Pick instances and click Generate.',
			'Loading open Glyphs font…',
		],
		'hull': hull,
	}


def render_dialog(state, anim_phase, out_path):
	root = DialogMockView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
	root.setMockState_(state)

	# Mount the real chart + specimen NSViews at the right Y positions
	# computed from the layout. These coordinates mirror the controller's
	# `_build_window` once it reaches Row 5 (Design space).
	#
	# Y walk through the layout:
	#   28 (title bar) + 16 (PAD) = 44
	#   + 14 (zone1 header)
	#   + ROW (28) + 6 (source radio row)
	#   + ROW (28) + 8 (font popup row)
	#   + 12 (divider1 gap)
	#   + 14 (zone2 header)
	#   + LABEL_H (20) + 6 (instances row)
	#   + 140 + 4 (list)
	#   + 14 + 4 (selection count)
	# = 44+14+34+36+12+14+26+144+18 = 342 — chart starts at y=342
	chart_y = 28 + PAD + 14 + (ROW + 6) + (ROW + 8) + 12 + 14 + (LABEL_H + 6) + 144 + 18
	col_w = W - CONTROL_X - PAD

	plot = make_hull_plot_view((CONTROL_X, chart_y, col_w, PLOT_H))
	axis_ranges = {
		'wght': (100.0, 400.0, 900.0),
		'opsz': (8.0, 12.0, 72.0),
	}
	axis_colors = {
		'wght': (0.46, 0.74, 1.00),
		'opsz': (1.00, 0.68, 0.42),
	}
	plot.setHull_axisRanges_axisColors_(state['hull'], axis_ranges, axis_colors)
	selected = sorted(state.get('focus_indices', []))
	plot.setInstances_selectedIndices_onClick_(state['instances'], selected, None)
	if 'wght' in state['hull'] and 'opsz' in state['hull']:
		wlo, whi = state['hull']['wght']
		olo, ohi = state['hull']['opsz']
		plot.setProbeCoords_({
			'wght': wlo + (whi - wlo) * anim_phase,
			'opsz': olo + (ohi - olo) * (1.0 - anim_phase),
		})
	root.addSubview_(plot)

	# Specimen sits PLOT_H + 16 (size estimate strip) + 22 (gap) below the
	# plot's top, so its top y = chart_y + PLOT_H + 38.
	specimen_y = chart_y + PLOT_H + 38
	preview = make_preview_view((CONTROL_X, specimen_y, col_w, SPECIMEN_H))
	preview.setFontSize_(40.0)
	preview.setHull_(state['hull'])
	preview._anim_progress = anim_phase * ANIM_PERIOD
	root.addSubview_(preview)

	# Composite path matches the glyphs render_dialog.
	window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
		NSMakeRect(0, 0, W, H), 0, NSBackingStoreBuffered, False,
	)
	try:
		window.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
	except (AttributeError, RuntimeError):
		pass
	window.contentView().addSubview_(root)
	try:
		root.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
	except (AttributeError, RuntimeError):
		pass

	transp = root.bitmapImageRepForCachingDisplayInRect_(root.bounds())
	root.cacheDisplayInRect_toBitmapImageRep_(root.bounds(), transp)

	dark = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
		None, W, H, 8, 4, True, False, 'NSCalibratedRGBColorSpace', 0, 0,
	)
	ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(dark)
	NSGraphicsContext.saveGraphicsState()
	NSGraphicsContext.setCurrentContext_(ctx)
	NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.14, 0.14, 1.0).set()
	NSRectFill(NSMakeRect(0, 0, W, H))
	img = NSImage.alloc().initWithSize_(NSMakeSize(W, H))
	img.addRepresentation_(transp)
	img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
		NSMakeRect(0, 0, W, H),
		NSMakeRect(0, 0, W, H),
		NSCompositingOperationSourceOver,
		1.0, True, None,
	)
	NSGraphicsContext.restoreGraphicsState()

	data = dark.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
	return bool(data.writeToFile_atomically_(out_path, True))


def main():
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument('--out', default=os.path.join(THIS_DIR, 'snapshots', 'dialog.png'))
	p.add_argument('--selected', default='1,2,8,18,19')
	p.add_argument('--anim', type=float, default=0.4)
	args = p.parse_args()

	os.makedirs(os.path.dirname(args.out), exist_ok=True)
	state = fake_state([int(x) for x in args.selected.split(',') if x.strip()])
	ok = render_dialog(state, args.anim, args.out)
	if ok:
		print(f'→ {args.out}  ({W}×{H})')
	else:
		print(f'! render failed for {args.out}')
		return 1
	return 0


if __name__ == '__main__':
	sys.exit(main())
