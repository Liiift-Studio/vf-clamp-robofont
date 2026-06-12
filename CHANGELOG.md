# Changelog

All notable changes to the vf-clamp RoboFont extension are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## 0.1.3 — 2026-06-11

Open-Font source feature wired in, controller refactor lands.

### Added
- **Open-Font source mode** — generate a restricted VF directly from a UFO that's
  already open in RoboFont. Detects the sibling `.designspace`, lists named
  instances from the XML for fast preview (no multi-second compile per click),
  then compiles via ufo2ft only at generation time. Source RadioGroup added
  to the UI; popup auto-populates from `AllFonts()`. Frontmost open font
  preselected.

### Changed
- `controller.py` now consumes `open_font_core` and `formats` end-to-end.
  Format dispatch routes through `formats.py` (`extension_for`, `flavor_for`,
  `inherits_ext`) — no more inline label-string comparisons in the controller.
- Source-mode is an explicit state machine: `_source_mode` ∈ {`SOURCE_FILE`,
  `SOURCE_OPEN_FONT`}, mutated only via `_transition_source_mode` which clears
  stale cross-source state.
- `_on_generate` split: input collection + validation extracted into
  `_collect_generate_inputs`; the handler now just dispatches by source mode.
- defcon/fontParts subsystem isolated in `open_font_core`; controller's
  fontTools path no longer touches font-source objects directly.

### Closed issues
- #44 — Hull preview now dispatches by source mode (designspace XML for
  open-font, compiled TTFont for file mode).
- #45 — Implicit source-mode state machine replaced with explicit
  `_source_mode` + `_transition_source_mode`.
- #46 — `_on_generate` no longer a god-method.
- #47 — fontTools / defcon subsystems separated via `open_font_core`.
- #48 — Format registry centralised in `formats.py`.
- #65 — Open-Font source feature ported.

## 2.1.1 — 2026-06-11

Architectural split following the cross-apply deep-review pass.

### Added
- `vf-clamp.roboFontExt/lib/vfClamp/formats.py` — central registry of output
  formats (TTF / OTF / WOFF / WOFF2) mirroring the Glyphs plugin's
  `formats.py` so dispatch logic lives in one place across the suite.
- `vf-clamp.roboFontExt/lib/vfClamp/open_font_core.py` — UFO / defcon /
  fontParts open-font subsystem split out from `controller.py` so the
  fontTools binary subsystem and the open-source UFO subsystem stay
  distinct.

### Notes
- New modules are standalone for this release; `controller.py` will be
  migrated to consume them in a follow-up.

## 2.1.0 — 2026-06-01

Major correctness and UX pass following the consolidated panel review.

### Fixed (CRITICAL)
- **WOFF/WOFF2 output now actually compresses**: `TTFont.save()` writes SFNT regardless of filename extension, so previous WOFF/WOFF2 selections produced silently-corrupt files. The format popup now sets `partial.flavor` before save. OTF↔TTF outline conversion is intentionally removed — output inherits the source SFNT type.
- **STAT table pruned after restriction**: `AxisValue` records pointing outside the restricted range are removed so OS font menus and CSS don't advertise unreachable styles.
- **fvar.instances pruned after restriction**: `fontTools.varLib.instancer` only removes pinned axes; range-restricted axes used to leave out-of-range named instances intact. Now explicitly filtered.

### Fixed (MAJOR)
- `info.plist` declares `requiresVersionMajor=4` so RoboFont 3 cannot install the extension.
- `info.plist` declares `html`, `license` keys; removes vestigial `mainScript`.
- `info.plist` `developerURL` now points at the studio homepage; `documentationURL` at the repo.
- Plugin version bumped from `0.1.0` to `2.1.0` to match upstream `@liiift-studio/vf-clamp`.
- `__init__.py` no longer instantiates the controller at import time — opens window only when executed as a menu script.
- Singleton pattern: second menu invocation focuses the existing window instead of opening a duplicate.
- `FloatingWindow` gets an `autosaveName` so position/size persist across launches.
- Window is now resizable; instance list grows with the window (`maxSize` 1200×1200).
- Cached `TTFont` reused between instance listing and generation — halves parse time and prevents file-handle leaks.
- Fonts whose instances have None / duplicate `subfamilyNameID` labels are now selectable via fallback to `postScriptNameID` and synthetic `Instance N` labels; duplicate labels disambiguated with `(2)` suffixes.
- Selection is keyed by `fvar.instances` index, not by debug name — eliminates the duplicate-name collision bug in `compute_hull`.
- `fvar.axes[i].defaultValue` is now reasserted into `(min, max)` after restriction, satisfying the OpenType `minValue ≤ defaultValue ≤ maxValue` invariant.
- `patch_name_table` now updates nameIDs 1, 2, 3, 4, 6, 16, 17, 25 (was only 1, 4, 6, sometimes 16, 25). Drops stale non-English localized records that would otherwise leak the original family name through locale-aware renderers. Drops platform-1 (Mac) records to avoid mac_roman lossy encoding.
- nameID 25 (Variations PS Name Prefix) is now spec-compliant: only `[A-Za-z0-9]`, ≤27 characters, must start with a letter.
- nameID 6 (PostScript name) enforces 63-char limit, collapses consecutive hyphens, strips leading non-letters.
- `head.fontRevision` bumped by 0.001 on every generation so font caches distinguish the derivative from the source.
- DSIG table stripped because instancing invalidates the signature.
- Generate button now disables during processing — no more double-click triggering concurrent runs.
- Overwrite confirmation dialog if the target file already exists.
- Errors are displayed in a multi-line `TextEditor` (was a single-line clipped `TextBox`).
- Output Family Name no longer overwrites user edits when the selection changes (dirty-flag tracking).
- "Select All" button added; first instance is auto-selected on font load to avoid first-time dead-ends.
- Computed axis range previewed in status area before generation.
- "Reveal in Finder" button surfaces the most recently written output.

### Fixed (MINOR)
- Filename sanitizer strips leading dots and `..` segments; output path is verified to stay inside the chosen folder.
- `os.path.getsize` upper-bound check (200 MB) on selected font.
- Runtime `fontTools.__version__` check at controller import; warns if older than 4.13.0.
- Narrowed `except Exception` to `except (ttLib.TTLibError, OSError)` for font loading; broader handler still catches unexpected errors but logs full traceback.
- Module-level `import traceback`; `print()` replaced with `logging.getLogger('vfClamp')` and `warnings.warn()` for the default-clamping notice.
- "Choose…" button relabeled "Choose Folder…" for screen-reader clarity.
- Generate button bound as default (Return key triggers generation).

### Known Limitations / Deferred
- ezui declarative layout: vanilla is still used; rewriting to `ezui.EZWindowController` is deferred as stylistic-only refactor.
- defcon/fontParts "use current font" integration: deferred; the workflow remains file-picker based.
- OS/2 `usWeightClass`/`usWidthClass`/`fsSelection` and `head.macStyle` recomputation: deferred — requires resolving "what is the canonical style identity of a multi-style restricted VF?", which has no obvious answer for a Light-Bold range.
- avar2 (OT 1.9+) detection/warning: deferred; current fontTools handles the common single-axis case.
- Type hints / `AxisConstraint` dataclass refactor: deferred as stylistic.
- README screenshots: deferred — requires running the live RoboFont app to capture.

## 0.1.0 — initial release
- Initial implementation: file picker, instance list, family-name rename, instancer call.
