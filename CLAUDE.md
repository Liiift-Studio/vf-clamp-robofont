# vf-clamp-robofont — Claude Code Configuration

## Inherited Context
This is a plugin submodule of `@liiift-studio/vf-clamp`. When working inside the
vf-clamp parent repo checkout, Claude Code will also load `vf-clamp/CLAUDE.md` which
defines the core purpose, API, name table patching approach, and shared conventions.

## What This Is
A RoboFont extension that generates restricted variable fonts from any TTF/OTF variable
font file. Font engineers select named instances, and the extension produces one
restricted VF with the correct name table.

## Tech Stack
- Python 3 (RoboFont built-in runtime)
- fonttools (bundled with RoboFont)
- vanilla (bundled with RoboFont, used for UI)
- RoboFont extension API (mojo.UI, mojo.extensions)

## Key Files
| File | Purpose |
|------|---------|
| `vf-clamp.roboFontExt/lib/vfClamp/__init__.py` | Extension entry, menu registration |
| `vf-clamp.roboFontExt/lib/vfClamp/controller.py` | UI controller and core logic |
| `vf-clamp.roboFontExt/info.plist` | Extension metadata |

## Installation
Double-click `vf-clamp.roboFontExt` or drag to the Extensions panel in RoboFont.

## Coding Standards
- Python 3 style
- Tabs for indentation
- One-line summary at top of each file
- Comment every function

## Engineers to Contact If Stuck
For RoboFont extension API: Frederik Berlaen (@typemytype), Erik van Blokland (@letterror).
For fonttools/instancer: Cosimo Lupo (@anthrotype), Behdad Esfahbod (@behdad).
