#!/usr/bin/env python3
# render_views.py — headless snapshot harness for the robofont vf-clamp
# extension. Renders just the two custom NSViews (hull plot + animated
# specimen) since the surrounding vanilla.FloatingWindow can't be built
# without RoboFont's runtime. The two shared modules are identical to the
# ones the Glyphs plugin ships, so this exists primarily so each robofont
# release has a paired visual artifact under versions/.

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
)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize  # noqa: E402

NSApplication.sharedApplication()

from hull_plot import make_hull_plot_view  # noqa: E402
from preview_view import make_preview_view, ANIM_PERIOD  # noqa: E402


# Same fixture as the glyphs harness so the two snapshots are directly
# comparable.
WEIGHTS = [
	('Thin', 190), ('Extralight', 204), ('Light', 219),
	('Italic', 235), ('Medium', 253), ('Semibold', 271),
	('Bold', 291), ('Extrabold', 313), ('Black', 336),
]
SIZES = [12, 24, 42, 60]


def synthetic_fixture():
	instances = []
	names = []
	for size in SIZES:
		for label, wght in WEIGHTS:
			instances.append({'wght': float(wght), 'opsz': float(size)})
			names.append(f'{size} {label} Italic')
	return instances, names


def hull_from(instances, selected):
	out = {}
	for idx in selected:
		for tag, val in instances[idx].items():
			if tag not in out:
				out[tag] = [val, val]
			else:
				out[tag][0] = min(out[tag][0], val)
				out[tag][1] = max(out[tag][1], val)
	return {tag: (lo, hi) for tag, (lo, hi) in out.items()}


def render_view_to_png(view, out_path, bg=(0.13, 0.13, 0.13, 1.0)):
	bounds = view.bounds()
	w, h = int(bounds.size.width), int(bounds.size.height)
	window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
		NSMakeRect(0, 0, w, h), 0, NSBackingStoreBuffered, False,
	)
	try:
		window.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
	except Exception:
		pass
	window.contentView().addSubview_(view)
	try:
		view.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
	except Exception:
		pass

	transp = view.bitmapImageRepForCachingDisplayInRect_(bounds)
	view.cacheDisplayInRect_toBitmapImageRep_(bounds, transp)

	dark = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
		None, w, h, 8, 4, True, False, 'NSCalibratedRGBColorSpace', 0, 0,
	)
	ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(dark)
	NSGraphicsContext.saveGraphicsState()
	NSGraphicsContext.setCurrentContext_(ctx)
	NSColor.colorWithCalibratedRed_green_blue_alpha_(*bg).set()
	NSRectFill(NSMakeRect(0, 0, w, h))
	img = NSImage.alloc().initWithSize_(NSMakeSize(w, h))
	img.addRepresentation_(transp)
	img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
		NSMakeRect(0, 0, w, h), NSMakeRect(0, 0, w, h),
		NSCompositingOperationSourceOver, 1.0, True, None,
	)
	NSGraphicsContext.restoreGraphicsState()

	data = dark.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
	return bool(data.writeToFile_atomically_(out_path, True))


def stack(top, bot, out_path, gap=12):
	t = NSImage.alloc().initWithContentsOfFile_(top)
	b = NSImage.alloc().initWithContentsOfFile_(bot)
	if t is None or b is None:
		return False
	ts, bs = t.size(), b.size()
	w = int(max(ts.width, bs.width))
	h = int(ts.height + bs.height + gap)
	bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
		None, w, h, 8, 4, True, False, 'NSCalibratedRGBColorSpace', 0, 0,
	)
	ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
	NSGraphicsContext.saveGraphicsState()
	NSGraphicsContext.setCurrentContext_(ctx)
	NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.13, 0.13, 1.0).set()
	NSRectFill(NSMakeRect(0, 0, w, h))
	b.drawAtPoint_fromRect_operation_fraction_(
		NSMakePoint((w - bs.width) / 2.0, 0),
		NSMakeRect(0, 0, bs.width, bs.height),
		NSCompositingOperationSourceOver, 1.0,
	)
	t.drawAtPoint_fromRect_operation_fraction_(
		NSMakePoint((w - ts.width) / 2.0, bs.height + gap),
		NSMakeRect(0, 0, ts.width, ts.height),
		NSCompositingOperationSourceOver, 1.0,
	)
	NSGraphicsContext.restoreGraphicsState()
	data = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
	return bool(data.writeToFile_atomically_(out_path, True))


def main():
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument('--out', default=os.path.join(THIS_DIR, 'snapshots', 'views.png'))
	p.add_argument('--selected', default='1,2,8,18,19')
	p.add_argument('--anim', type=float, default=0.4)
	p.add_argument('--width', type=int, default=406)
	args = p.parse_args()

	os.makedirs(os.path.dirname(args.out), exist_ok=True)

	instances, _names = synthetic_fixture()
	selected = [int(x) for x in args.selected.split(',') if x.strip()]
	hull = hull_from(instances, selected)
	axis_ranges = {
		'wght': (100.0, 400.0, 900.0),
		'opsz': (8.0, 12.0, 72.0),
	}
	axis_colors = {
		'wght': (0.46, 0.74, 1.00),
		'opsz': (1.00, 0.68, 0.42),
	}

	plot = make_hull_plot_view((0, 0, args.width, 140))
	plot.setHull_axisRanges_axisColors_(hull, axis_ranges, axis_colors)
	plot.setInstances_selectedIndices_onClick_(instances, selected, None)
	if 'wght' in hull and 'opsz' in hull:
		wlo, whi = hull['wght']
		olo, ohi = hull['opsz']
		plot.setProbeCoords_({
			'wght': wlo + (whi - wlo) * args.anim,
			'opsz': olo + (ohi - olo) * (1.0 - args.anim),
		})
	plot_png = args.out.replace('.png', '-plot.png')
	render_view_to_png(plot, plot_png)

	preview = make_preview_view((0, 0, args.width, 80))
	preview.setFontSize_(40.0)
	preview.setHull_(hull)
	preview._anim_progress = args.anim * ANIM_PERIOD
	preview_png = args.out.replace('.png', '-preview.png')
	render_view_to_png(preview, preview_png)

	stack(plot_png, preview_png, args.out)
	print(f'→ {args.out}')
	return 0


if __name__ == '__main__':
	sys.exit(main())
