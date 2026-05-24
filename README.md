# vf-clamp — RoboFont Extension

Generate restricted variable fonts from named instance ranges. Per-purchase micro-VF delivery for type foundries.

## What It Does

Select a variable font file, pick one or more named instances, and the extension produces a restricted VF that spans exactly that axis range — with the name table updated to reflect the purchased instances.

**Example:** a customer who buys "Light" and "Bold" receives a VF spanning `wght 300–700`, named *Typeface Light-Bold*, not the full family.

This is the native RoboFont version of the [`@liiift-studio/vf-clamp`](https://github.com/Liiift-Studio/vf-clamp) npm package. It calls `fontTools.varLib.instancer` directly — no Node.js or npm required.

## Requirements

- **RoboFont 4** or later
- **Python 3** — bundled with RoboFont
- **fontTools** — bundled with RoboFont
- **vanilla** — bundled with RoboFont

No additional dependencies needed.

## Installation

1. Download or clone this repository.
2. Double-click `vf-clamp.roboFontExt` — RoboFont will install it automatically.
   Or drag the `.roboFontExt` bundle into **Extensions > Show Extensions Folder**.
3. Restart RoboFont (or reload extensions).

## Usage

1. Go to **Extensions > Generate Restricted VFs…**
2. Click **Select Font…** and choose a variable `.ttf` or `.otf` file.
3. Select one or more named instances from the list.
4. Edit the **Output Family Name** if needed (auto-filled from your selection).
5. Choose an output **Format**: TTF, OTF, WOFF, or WOFF2.
6. Optionally choose a different **Output Folder** (defaults to the font's folder).
7. Click **Generate**.

The restricted VF is written to the output folder immediately.

## How It Works

- **Axis hull computation:** the extension calculates the min/max value per axis across all selected named instances and uses `instancer.AxisRange` to restrict the font to that range.
- **Name table patching:** nameID 1, 4, 6 (and 16/25 if present) are updated so the output file self-identifies with the correct family name.
- **Compact naming:** selecting "Inter Light" and "Inter Bold" produces the name "Inter Light-Bold" by stripping shared prefix/suffix words.

## Links

- [vfclamp.com](https://vfclamp.com)
- [vf-clamp npm package](https://github.com/Liiift-Studio/vf-clamp)
- [Liiift Studio](https://liiift.studio)

## License

Proprietary — Liiift Studio. All rights reserved.
