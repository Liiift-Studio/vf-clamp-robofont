# Changelog

## 1.2.2 — 2026-06-14

### Changed
- **Chart pushed down 14 px** so the y_max corner label has breathing room above the chart border instead of cramping the "Design space:" label above it. Same fix as glyphs v1.2.19.

## 1.2.1 — 2026-06-14

### Changed
- **Synced hull_plot.py** with the v1.2.18 glyphs fix: corner numeric labels are now OUTSIDE the chart border (top-left, bottom-left, bottom-right) instead of inside. Eliminates the dot overlap users saw on real fonts. Pushed through `npm run sync-plugin-views` from `vfClamp/shared/plugin-views/`.

### Added
- **`tools/render_dialog.py`** — full-window FloatingWindow mock (chrome + real custom NSViews) at 620 × 920, mirroring v1.2.0 layout. Sibling to the views-only `render_views.py`. Used to render the screenshot for the site cards on vfclamp.com.

## 1.2.0 — 2026-06-14

### Added
- **Preset popup** in the OUTPUT section: save the current instance selection by name, recall it on demand, manage saved entries. Persistence is a single JSON file at `~/.vf-clamp/presets.json`. Presets are name-portable — applying one on a different source picks up any instances whose name matches and reports anything missing in the log.
- **Three zone headers** — small uppercase `SOURCE`, `DASHBOARD`, `OUTPUT` labels at the top of each section. Lightweight visual cue that the dialog has three distinct sections, matching the Glyphs plugin's three-zone rhythm without the layout-coupling risk of full `vanilla.Box` wrappers.

### Changed
- Window grew 760 → 834 px to fit the new preset row + zone headers.

## 1.1.0 — 2026-06-13

Full UI parity pass with `vf-clamp-glyphs` v1.2.17. v1.0.0 brought the design-space chart + animated specimen; v1.1.0 backports the remaining UX features the Glyphs plugin accumulated across v1.2.6 → v1.2.17.

### Added
- **Scrollable LOG pane** between the output section and the action bar (84 px tall, monospaced TextEditor). Replaces the prior single-line statusLabel — error tracebacks now have room to breathe and stay readable.
- **Activity stripe** on the LOG's left edge: a 3-px accent NSView (`_LogActivityStripe`) that flashes at 100% then fades to 0% over 0.8 s every time a new line lands. Peripheral cue so users notice async status without focus-stealing alerts.
- **Filter field** above the instance list — case-insensitive substring filter. The filter+selection paths now translate filtered indices back to full-list indices before computing hulls, so the algorithm always sees the right instances.
- **Selection-count line** below the list (`5 of 36 selected`).
- **More popup** alongside All / None / Invert: smart selects for "Select All Italic" / "Select All Roman" by substring matching on instance names.
- **Open after generating checkbox** — when checked (default), the produced .ttf/.otf opens in the OS default app via `NSWorkspace.openFile_` immediately after a successful save.
- **Shortcut chips strip** on the left side of the action bar: `⌘A All   ⌘D None   ⌘I Invert   ⇥ Navigate   ␣ Toggle   ⏎ Generate`. secondaryLabelColor so they read on dark RoboFont panels.
- **Keyboard shortcut monitor** — local `NSEvent` monitor handles ⌘A / ⌘D / ⌘I when the dialog has focus (and a text field isn't editing). Torn down on window close so a closed dialog doesn't leak a global Cmd-A/D/I hook.
- **Animated specimen timer cleanup** on window close so an orphaned `NSTimer` doesn't drive draws into a destroyed view.

### Changed
- **Window grew from 560×640 → 620×760** to accommodate the LOG pane + wider shortcut chip strip.
- **`statusLabel`** is now a 1×1 hidden placeholder; legacy `self.w.statusLabel.set(...)` calls won't crash but flow through `_set_status → _log_append` instead.

## 1.0.0 — 2026-06-13

Major version jump signaling feature parity with `vf-clamp-glyphs` v1.2.17. The robofont extension was at v0.1.x with a single-line axis-chips preview; v1.0.0 brings the full interactive design-space chart, animated HOHO Anes specimen, structural counters, accessibility annotations, and shared NSView modules from the Glyphs plugin.

### Added
- **Interactive design-space chart** (replaces the old single-line axis chips). Per-instance dots, hull rectangle, axis tick marks + numeric corner labels, live probe ring, keyboard focus ring, per-dot VoiceOver children. Mounted via the framework-agnostic `hull_plot.py` module shared with the Glyphs plugin.
- **Animated HOHO Anes specimen** at 40 pt — sweeps through the licensed design space at 30 fps, paired with the probe ring above. Caption shows live variation values. From shared `preview_view.py`.
- **Structural counters** in the new size-estimate strip: `~38 KB  ·  5 instances  ·  5 masters  ·  2 ax  ·  1 pinned`. The masters count comes from walking `designspace.sources` and filtering those whose location falls inside the hull range on every axis.
- **Per-build snapshot versioning** under `versions/views-v$VERSION.png` — `scripts/build-zip.sh` renders it as the last step. `tools/render_views.py` renders the two custom views standalone (the full FloatingWindow needs RoboFont runtime).
- **`scripts/build-zip.sh`** — deterministic zip build (mirrors the Glyphs plugin's reproducibility pattern): version-parity check, untracked-file refusal, normalized mtimes, SHA-256 checksum.

### Changed
- **User-facing terminology**: "hull preview" → "design space" / "design space preview" in label, tooltip, and accessibility strings. Code-internal `hull` / `compute_hull` names retained.
- **Window grew from 480 → 640 px tall** to accommodate the new chart + specimen. Width unchanged at 560.

All notable changes to the vf-clamp RoboFont extension are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## 0.1.4 — 2026-06-12

Final pass on the cross-apply deep-review backlog — closes the last 16
deferred findings across quick wins, font correctness, and partial sweeps.

### Added
- `scripts/check-version-parity.sh` — refuses to build/tag when `info.plist`,
  `README.md`, and `CHANGELOG.md` disagree on the plugin version. Guards
  against the tag-vs-binary drift the Glyphs sibling has had to fix twice.
- `tests/test_open_font_core.py` — direct coverage for the `open_font_core`
  subsystem (designspace XML parsing, instance enumeration, ufo2ft compile
  shim) so the open-font path has unit tests independent of RoboFont.
- `tests/__init__.py` so the test directory is importable as a package and
  pytest's discovery is deterministic across CI and local runs.

### Changed
- `controller.py` absorbs 419 lines of partial-sweep follow-up across
  validation, error-path UX, designspace caching, and a narrower
  `except Exception` audit — the changes are scoped to the controller layer
  so the fontTools / open-font core modules remain unchanged.
- `open_font_core.py` gains 44 lines of follow-up: explicit type hints on
  the public surface, clearer error messages when defcon / fontParts can't
  resolve the open font, and a small amount of extra defensive logging on
  the ufo2ft compile path.

### Closed findings
- 16 deferred findings from the cross-apply deep-review pass — quick wins
  on the controller, partial font-correctness sweeps for the open-font
  source path, and the tooling/version-parity guard above.

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
