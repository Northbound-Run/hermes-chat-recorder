# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project adheres to [Semantic Versioning](https://semver.org/)
(pre-1.0: minor bumps may break).

## [Unreleased]

## [0.7.4] - 2026-06-24

### Added
- Community-health files: `CONTRIBUTING.md`, `SECURITY.md` (private vulnerability
  reporting via GitHub Security advisories), `.github/ISSUE_TEMPLATE/` (bug +
  feature + routing `config.yml`), and `.github/PULL_REQUEST_TEMPLATE.md`.
- `docs/releasing.md` documenting the release process.
- `HERMES_PLUGIN_STANDARD.md` — the shared conventions Northbound's Hermes
  plugins follow (a canonical doc kept identical across plugin repos), linked
  from the README.

### Changed
- **Python floor raised to 3.11** (`requires-python = ">=3.11"`). The CI matrix
  is now 3.11–3.14 (dropped 3.10), and the 3.10 classifier, ruff
  `target-version`, and mypy `python_version` were updated to match.
- **PEP 639 license metadata** in `pyproject.toml`: `license = "MIT"` +
  `license-files = ["LICENSE"]`, with `[build-system] requires = ["setuptools>=77"]`
  (emits Metadata-Version 2.4).
- **Release workflow** switched to the standard tag-push pattern
  (`.github/workflows/release.yml`, replacing `publish.yml`): triggers on a
  pushed `vX.Y.Z` tag, asserts the tag matches `project.version`, runs
  `twine check --strict`, offers a `workflow_dispatch` TestPyPI dry-run, and
  publishes via OIDC Trusted Publishing using `pypi`/`testpypi` environments.
- `[project.urls]` casing corrected to the `Northbound-Run` GitHub org and a
  `Documentation` URL added; the README CI badge link was fixed to match.
- Development Status classifier bumped to `4 - Beta` (and the README status note
  softened from "alpha" to "beta") to match "used in production".

## [0.7.3] — 2026-06-23

### Added
- **One-line install via the Hermes plugin manager.** `hermes plugins
  install Northbound-Run/hermes-chat-recorder --enable` now works
  alongside the existing `pip install hermes-chat-recorder` path. A
  repo-root `plugin.yaml` (the manifest the git-clone installer reads)
  plus a thin repo-root `__init__.py` shim — which puts `src/` on
  `sys.path` and re-exports `register()` — let Hermes's directory
  loader find a working entry point at the clone root without
  abandoning the src-layout PyPI package. If both installs are present
  they collide on the `chat_recorder` key and the entry-point copy
  wins, so pick one path.
- README restructured around Quick Start / Updating / Documentation /
  Project layout, mirroring the conventions of other Hermes plugins.

### Fixed
- **Corrupted `plugin.yaml` manifest.** A stray global replace of the
  token `version` had mangled the manifest into invalid YAML
  (`version: 0.7.2` had become ` 0.7.1:version: 0.7.2: 0.7.1`);
  rewritten clean.

### Changed
- Dev tooling: `pytest` now runs in `--import-mode=importlib` and mypy
  gains `explicit_package_bases` / `mypy_path = src`. Both are required
  so the new repo-root `__init__.py` (the directory-install entry shim)
  doesn't break test collection or type checking — the repo directory
  name contains a hyphen, which the default `__init__.py` walk-up
  rejects as an invalid package name.

## [0.7.2] — 2026-06-11

### Fixed
- **Signal group IDs no longer collapse to `group/`**: the ID-slug
  fallback split every chat ID at the first colon (a Matrix-ism), so a
  hint-less write for a Signal group (`group:<base64>`) landed in a
  shared `group/` folder. The `:server` strip now applies only to
  sigil-prefixed Matrix IDs, and the hint and fallback paths produce
  the same folder for Signal groups.

## [0.7.1] — 2026-06-11

### Fixed
- **Signal messages were not recorded at all**: Hermes's Signal adapter
  never sets `MessageEvent.message_id` (the protocol identifies a
  message by sender + timestamp), and the extractor treated a missing
  ID as unrecordable. A stable ID is now synthesized from the sender
  and the platform's millisecond timestamp (`syn:<sender>:<ts_ms>`),
  preserving redelivery dedupe. Applies to any adapter that omits
  `message_id`.

## [0.7.0] — 2026-06-11

First public release.

### Added
- **All-platform recording.** The recorder now archives every channel
  the Hermes gateway dispatches — Telegram, Discord, Slack, Signal,
  WhatsApp, IRC, email, and the rest — not just Matrix. Generic
  extraction reads Hermes's unified `MessageEvent`/`SessionSource`
  fields; Matrix keeps an enrichment pass (server timestamps, mxc
  provenance, edit/reaction guards).
- Outbound recording on **every** platform adapter (previously Matrix
  only). Failed sends (`SendResult.success == False`) are not recorded.
- New recorded kinds: `video`, `file` (documents), `location` — stored
  as-is at the new terminal stage `recorded`, no processing.
- `platforms` config — allowlist of platforms to record (default: all).
- `bot_name` config — display name for outbound sections.
- Chat/sender names from event metadata (`chat_name` / `user_name`)
  on all platforms; Matrix client lookups demoted to fallback.
- `py.typed` marker — the package ships inline type hints.

### Changed
- **Vault layout (breaking):** group mode now writes
  `<vault_root>/<platform>/<chat-slug>/<YYYY-MM-DD>.md`. Migrate an
  existing Matrix-only vault with
  `mkdir -p <vault_root>/matrix && mv <vault_root>/<room> <vault_root>/matrix/`.
  Flat (`bot_type: "1on1"`) layouts are unaffected.
- `plugin.yaml` manifest now uses the `provides_hooks` key Hermes
  actually parses (the previous `hooks:` key was ignored), and drops
  unparsed keys.
- The hook callback accepts `**kwargs` for forward compatibility with
  future Hermes hook arguments.
- Internal API renames for the platform-generic model:
  `matrix_event.py` → `events.py`, `MatrixEventInfo` → `EventInfo`,
  `NameResolver.room_slug(id)` → `chat_slug(platform, id, hints…)`,
  `Recorder.record_outbound(room_id=…)` → `(platform=…, chat_id=…)`.

### Removed
- Dead `mentioned_mxids` extraction (a leftover of the pre-0.4 wake
  gate).

## [0.6.0] — 2026-06-05
- Matrix edit-event (`m.replace`) handling: edits get their own
  `stage:edited` section linked to the original via `edits:`.
- Code-review fixes.

## [0.5.1] — 2026-06-03
- Fix outbound `send` wrapper signature — Hermes passes the message
  body as keyword `content`, not `text`.

## [0.5.0] — 2026-06-02
- `bot_type` config: `"1on1"` flat vault layout for single-DM bots.

## [0.4.2] — 2026-06-01
- Use `MessageEvent.media_urls` (adapter-cached local files) for voice
  and image bytes instead of re-downloading via mxc.

## [0.4.1] — 2026-05-31
- Fix Matrix adapter discovery — `GatewayRunner.adapters` is keyed by
  the `Platform` enum, not by string.

## [0.4.0] and earlier
- Internal iterations: wake-gate removal (recording-only design),
  delegation of STT/vision to Hermes's built-in tools, stage-based
  idempotent vault writer, Matrix name resolution.
