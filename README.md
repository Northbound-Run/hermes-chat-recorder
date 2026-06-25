# hermes-chat-recorder — Hermes Plugin

[![PyPI](https://img.shields.io/pypi/v/hermes-chat-recorder)](https://pypi.org/project/hermes-chat-recorder/)
[![CI](https://github.com/Northbound-Run/hermes-chat-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/Northbound-Run/hermes-chat-recorder/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/hermes-chat-recorder)](https://pypi.org/project/hermes-chat-recorder/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Record every chat message the Hermes gateway sees — on any connected platform (Matrix, Telegram, Discord, Slack, Signal, WhatsApp, IRC, email, …) — into an Obsidian-style Markdown vault.** Voice notes are transcribed and images are described inline using whatever STT and vision providers Hermes is already configured for. No databases, no sidecar indexes, no third-party API keys.

`hermes-chat-recorder` adds one thing to Hermes: a single `pre_gateway_dispatch` hook that archives traffic to a greppable vault. It is **recording-only** — it never decides whether the agent wakes up.

> **Status: beta (pre-1.0).** APIs and config schema may change.
> Used in production at Northbound.

---

## Why this exists

Most chat-archival setups fail in one of two boring ways: they bolt onto a single platform, or they drag in their own transcription/vision stack with its own keys to rotate. This plugin is delegation-based instead:

- **Any platform, one hook.** Every Hermes adapter normalizes to a unified `MessageEvent`, so the same recorder captures Matrix, Telegram, Discord, Slack, Signal, WhatsApp, IRC, and email without per-platform code.
- **No third-party deps.** STT and vision are delegated to Hermes's built-in tools — local faster-whisper, Groq, OpenAI, Mistral, or xAI for speech; the main LLM or an auxiliary vision provider for images. The only runtime requirement is Hermes itself.
- **Recording-only.** It does NOT gate the agent's wake — use Hermes's native mention/allowlist settings for that. It records *before* Hermes's auth/pairing checks, so messages from unpaired senders land in the vault too. **Treat the vault path as sensitive.**
- **Pretty names for free.** Folders come out as `telegram/Family-Chat/` and headers as `### 09:14 Annika · voice (0:12)`, resolved from adapter metadata (and, on Matrix, live room/profile lookups).
- **Greppable, not a database.** Per-platform, per-chat, per-day Markdown files with HTML-comment anchors for idempotency. Obsidian-friendly, diff-friendly, no migrations.

---

## Quick Start

```bash
# 1) Install + enable the plugin (pick ONE path)

#    A. Hermes plugin manager — git-clones into ~/.hermes/plugins/chat_recorder
hermes plugins install Northbound-Run/hermes-chat-recorder --enable

#    B. PyPI — auto-discovered via the hermes_agent.plugins entry point
pip install hermes-chat-recorder
hermes plugins enable chat_recorder
```

```yaml
# 2) Configure: add a chat_recorder block to your Hermes config.yaml.
#    The only setting you really want is vault_root (defaults to
#    /data/vault/transcripts if omitted).
plugins:
  enabled:
    - chat_recorder           # `--enable` / `enable` already added this
  chat_recorder:
    enabled: true
    vault_root: /data/vault/transcripts
    record_outbound: true
    timezone: America/Los_Angeles
```

```bash
# 3) Restart the gateway so the plugin loads and wires the adapters.
#    (Recording begins with the first message after startup.)

# 4) Verify it registered
hermes plugins list            # chat_recorder should be listed + enabled
```

Notes:

- **Path A** clones this repo into `~/.hermes/plugins/chat_recorder/` (the directory name comes from the manifest `name:`, which is why `--enable` adds `chat_recorder`). Without `--enable` the installer prompts *"Enable now? [y/N]"*.
- **Path B** installs from PyPI; Hermes discovers it through the `hermes_agent.plugins` entry-point group. Use this when the plugin lives in the same venv as Hermes.
- Don't run both paths at once. If a directory clone and the PyPI package are both present they collide on the `chat_recorder` key, and Hermes loads the **entry-point (PyPI) copy** — entry-points are merged last, so they win the collision and the clone is silently ignored. Pick the one that matches how you deploy Hermes.
- **The vault holds unredacted message content** (including from unpaired senders, recorded before auth). Put `vault_root` somewhere access-controlled.
- Python 3.11+ is required. The plugin pulls in **no** third-party runtime dependencies.

---

## Updating

```bash
# Path A (plugin manager)
hermes plugins update chat_recorder

# Path B (PyPI)
pip install -U hermes-chat-recorder
```

Restart the gateway afterward so the updated plugin code is reloaded. See [Upgrading from ≤ 0.6.x](#upgrading-from--06x) if you are crossing the v0.7.0 vault-layout change.

---

## Documentation

- [`docs/DESIGN.md`](docs/DESIGN.md) — full architecture: event normalization, the recorder pipeline, adapter wiring, name resolution, and failure policy.
- [`docs/releasing.md`](docs/releasing.md) — how to cut a release (tag-push + Trusted Publishing).
- [`CHANGELOG.md`](CHANGELOG.md) — version history (Keep a Changelog format; pre-1.0 minor bumps may break).
- [`HERMES_PLUGIN_STANDARD.md`](HERMES_PLUGIN_STANDARD.md) — the shared conventions Northbound's Hermes plugins follow (identity, packaging, security, CI, release).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, conventions, and the PR checklist.
- [`SECURITY.md`](SECURITY.md) — security model and how to report a vulnerability privately.
- [Vault format](#vault-format) — the on-disk Markdown layout and idempotency model.
- [Compatibility notes & gotchas](#compatibility-notes--gotchas) — adapter mention-gating, plugin load timing, and outbound-capture caveats.
- [Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin) — upstream reference for plugin discovery and debugging.

---

## What gets recorded

| Inbound | Behaviour |
|---|---|
| Text | Append the message to `vault_root/<platform>/<chat>/<YYYY-MM-DD>.md`. Pass through unmodified — Hermes's normal wake settings decide whether the agent replies. |
| Voice note | Transcribe via Hermes's built-in STT (`tools.transcription_tools`). Append placeholder + transcript section. Rewrite `event.text` to the transcript so the agent has usable content if it wakes. |
| Image / sticker | Describe via Hermes's built-in vision (`tools.vision_tools`). Append placeholder + description section. Rewrite `event.text` to caption + description + OCR'd text. |
| Video / file / location | Recorded as-is (caption + media provenance), no processing. |
| Reaction | Ignored (not recorded as a message section). |
| Outbound (the agent's own replies, every platform) | Recorded with a `reply_to:` link back to the trigger when available. |

---

## Configuration

The plugin reads the `plugins.chat_recorder` block in your Hermes `config.yaml`:

```yaml
plugins:
  enabled:
    - chat_recorder
  chat_recorder:
    enabled: true
    vault_root: /data/vault/transcripts
    record_outbound: true
    timezone: America/Los_Angeles

    # Optional: restrict recording to specific platforms.
    # Empty / omitted = record everything the gateway dispatches.
    platforms: []          # e.g. [matrix, telegram]

    # Optional: display name for the bot's own outbound sections.
    # Default resolves via the bot's Matrix profile, else "bot".
    bot_name: ""

    # "group" (default) keeps the per-platform/per-chat subfolders.
    # "1on1" assumes the bot only ever lives in a single DM and
    # flattens to <vault_root>/<YYYY-MM-DD>.md — no subfolders.
    bot_type: "group"

    # Optional: manual name overrides, keyed by raw platform IDs.
    # Useful when a Signal/WhatsApp bridge user has no display name,
    # or to rename a chat's folder. Overrides win over all lookups.
    name_overrides:
      rooms:
        "!abcdef1234:example.org": "Family Signal"
      users:
        "@signal_2c991545-...:example.org": "Annika"
```

STT and vision are configured **at the Hermes top level**, not here:

```yaml
stt:
  enabled: true
  provider: "local"   # or "groq" / "openai" / "mistral" / "xai"
  local:
    model: "base"

auxiliary:
  vision:
    provider: "main"  # use the main LLM, or override per Hermes docs
    model: ""
```

Optional env vars:

- `TRANSCRIPT_TZ` — overrides `timezone`.

---

## Vault format

Per-platform, per-chat, per-day files at
`<vault_root>/<platform>/<chat-slug>/<YYYY-MM-DD>.md`, sections
separated by `---`, each anchored by an HTML comment:

```markdown
<!-- event:$abcd1234:server -->
### 09:14 Annika · voice (0:12) · stage:transcribed
**mxc:** mxc://server/abcdef
**duration_sec:** 12

> okay so the deck was titled "what ai can do for your business" and
> we showed it to the meridian team last thursday

---

<!-- event:$efgh5678:server -->
### 09:14 Recorder · reply · stage:sent
**reply_to:** $abcd1234:server

You bet — the AI-for-business one. I'll drop a refresher in your daily note.

---
```

Idempotency comes from the HTML-comment anchor — re-delivery of the
same event ID finds the existing section and either updates its stage
in-place or no-ops if already at a terminal stage. The vault stays
greppable and Obsidian-friendly: no databases, no sidecar indexes.

---

## Upgrading from ≤ 0.6.x

v0.7.0 added the platform folder level. Existing Matrix-only vaults
move with one command:

```bash
mkdir -p <vault_root>/matrix && mv <vault_root>/<each-room-folder> <vault_root>/matrix/
```

(`bot_type: "1on1"` flat layouts are unaffected.)

---

## Compatibility notes & gotchas

**Adapter-level mention gating hides messages from the recorder.**
Some Hermes adapters can drop unmentioned group messages *inside the
adapter*, before any plugin hook runs — e.g. Signal's
`require_mention` / `SIGNAL_REQUIRE_MENTION`. With that enabled, gated
messages are never dispatched and therefore never recorded. If you
want "archive everything, reply only when mentioned", leave
adapter-level gating off and gate the *wake* instead: a small
companion plugin that returns `{"action": "skip"}` from
`pre_gateway_dispatch` for unmentioned group messages. Hermes runs all
hook callbacks before acting on any result, so the recorder archives
the message either way. (Caveat for entry-point distribution: the
gateway honors the first `skip` and stops at a `rewrite`, so a gate
plugin should load before this one — install it as a user-dir plugin
under `~/.hermes/plugins/`, which always loads ahead of pip
entry-points.)

**Plugin load timing on older Hermes.** Current Hermes calls
`discover_plugins()` at gateway startup, so recording begins with the
first message. Older versions loaded entry-point plugins lazily on the
first agent turn — messages before that were invisible to the hook. On
such versions, add a `gateway:startup` hook that calls
`hermes_cli.plugins.discover_plugins()` (see Hermes's gateway event
hooks docs) to load plugins at boot.

**Outbound capture starts at the first inbound dispatch.** The `send`
wrappers are wired lazily when the first message flows through the
gateway, so bot messages sent before that moment in a fresh process
(e.g. startup broadcasts) may be missed or, when no chat name is known
yet, filed under an ID-derived folder name until a message in that
chat establishes the pretty name.

---

## Troubleshooting

Plugin not showing up? Hermes has verbose discovery logs:

```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

Common causes: the plugin isn't in `plugins.enabled`, or the gateway
wasn't restarted after install. See the
[Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin#debugging-plugin-discovery)
for the full debugging walkthrough.

---

## Local development

```bash
git clone https://github.com/Northbound-Run/hermes-chat-recorder
cd hermes-chat-recorder
pip install -e .[dev]

pytest -q          # 300+ unit tests, no network
ruff check .
mypy src
```

The test suite runs entirely offline. `pytest` uses
`--import-mode=importlib` (set in `pyproject.toml`) so the repo-root
`__init__.py` — the directory-install entry shim — doesn't interfere
with test collection.

---

## Project layout

```text
.
├── plugin.yaml             Manifest read by the `hermes plugins install` git-clone path
├── __init__.py             Repo-root entry shim: puts src/ on sys.path, re-exports register()
├── pyproject.toml          Package metadata, hermes_agent.plugins entry point, tooling
├── README.md
├── CHANGELOG.md
├── LICENSE
├── docs/
│   └── DESIGN.md           Full architecture & design rationale
├── src/
│   └── hermes_chat_recorder/
│       ├── __init__.py     Public surface — register()
│       ├── plugin.py       register(ctx): config load + hook/adapter wiring
│       ├── recorder.py     Per-event orchestration (placeholder → finalize)
│       ├── writer.py       Vault file writer: sections, anchors, idempotency
│       ├── events.py       MessageEvent → typed EventInfo normalization
│       ├── transcriber.py  Voice notes → Hermes STT
│       ├── describer.py    Images → Hermes vision
│       ├── name_resolver.py Chat/user pretty-name resolution + cache
│       ├── config.py       Config block parsing & validation
│       ├── types.py        Shared dataclasses / enums
│       ├── _background_loop.py  Async helper for off-thread work
│       └── plugin.yaml     Manifest (wheel copy; informational on the pip path)
└── tests/                  pytest suite
```

Two manifests are intentional: the **repo-root** `plugin.yaml` is what
the git-clone installer reads, while the **packaged** copy under
`src/` ships in the wheel for reference (the pip/entry-point path
builds its manifest from the entry-point name and doesn't parse it).
Keep them in sync.

---

## Architecture in one paragraph

The plugin registers a single `pre_gateway_dispatch` hook. Inbound, it
normalizes Hermes's unified `MessageEvent` (any platform) into a typed
`EventInfo`, writes a placeholder section, runs voice/image events
through Hermes's STT/vision tools, finalizes the section, and rewrites
`event.text` so the agent sees the transcript/description. On the
first dispatch it also wraps `send` on **every** live platform adapter
so outbound replies land in the vault; the Matrix adapter additionally
contributes its client for name lookups and a media-download fallback.
Failure policy: per-event errors degrade to `*_failed` sections;
startup/config errors fail registration loudly. See
[`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

---

## License

[MIT](LICENSE) — Copyright (c) 2026 Northbound.

## Related

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent runtime this plugin extends ([docs](https://hermes-agent.nousresearch.com)).
- [hermes-chat-recorder on PyPI](https://pypi.org/project/hermes-chat-recorder/) — the published package for the pip install path.
