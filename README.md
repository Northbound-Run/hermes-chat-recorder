# hermes-chat-recorder

[![PyPI](https://img.shields.io/pypi/v/hermes-chat-recorder)](https://pypi.org/project/hermes-chat-recorder/)
[![CI](https://github.com/northbound-run/hermes-chat-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/northbound-run/hermes-chat-recorder/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/hermes-chat-recorder)](https://pypi.org/project/hermes-chat-recorder/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that
records every chat message the gateway sees — on **any connected
platform** (Matrix, Telegram, Discord, Slack, Signal, WhatsApp, IRC,
email, …) — into an Obsidian-style Markdown vault. Voice notes are
transcribed and images are described inline, using whatever STT and
vision providers Hermes is already configured for.

> **Status: alpha (pre-1.0).** APIs and config schema may change.
> Used in production at Northbound.

## What it does

| Inbound | Behaviour |
|---|---|
| Text | Append the message to `vault_root/<platform>/<chat>/<YYYY-MM-DD>.md`. Pass through unmodified — Hermes's normal wake settings decide whether the agent replies. |
| Voice note | Transcribe via Hermes's built-in STT (`tools.transcription_tools`). Append placeholder + transcript section. Rewrite `event.text` to the transcript so the agent has usable content if it wakes. |
| Image / sticker | Describe via Hermes's built-in vision (`tools.vision_tools`). Append placeholder + description section. Rewrite `event.text` to caption + description + OCR'd text. |
| Video / file / location | Recorded as-is (caption + media provenance), no processing. |
| Reaction | Ignored (not recorded as a message section). |
| Outbound (the agent's own replies, every platform) | Recorded with a `reply_to:` link back to the trigger when available. |

This plugin is **recording-only**. It does NOT decide whether the
agent wakes up — use Hermes's native mention/allowlist settings for
that. It also records *before* Hermes's auth/pairing checks, so
messages from unpaired senders land in the vault too; treat the vault
path as sensitive.

**Pretty names for free.** Every Hermes adapter ships the chat title
and sender display name on the unified event (`source.chat_name` /
`source.user_name`), so folders come out as `telegram/Family-Chat/`
and headers as `### 09:14 Annika · voice (0:12)`. On Matrix the plugin
additionally queries the live client (`m.room.name`, profile display
names, DM peers) for rooms the event metadata doesn't cover. Unnamed
DMs use the peer's name. All resolutions are cached for the process
lifetime — restart the gateway to pick up renames.

**No third-party deps.** STT and vision are delegated to Hermes's
built-in tools, so whatever provider Hermes is configured for — local
faster-whisper, Groq, OpenAI, Mistral, xAI for STT; the main LLM or an
auxiliary vision provider for images — is what the recorder uses. The
only runtime requirement is Hermes itself.

## Install

```bash
pip install hermes-chat-recorder
```

Hermes auto-discovers pip-installed plugins via the
`hermes_agent.plugins` entry-point group. Then enable it:

```bash
hermes plugins enable chat_recorder
```

(or add it to `plugins.enabled` in `config.yaml` by hand, as below).
Restart the gateway and you should see `chat_recorder` in
`hermes plugins list`.

For development against an unreleased version:

```bash
git clone https://github.com/northbound-run/hermes-chat-recorder
cd hermes-chat-recorder
pip install -e .[dev]
```

## Configure

Add to your Hermes `config.yaml`:

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

## Upgrading from ≤ 0.6.x

v0.7.0 added the platform folder level. Existing Matrix-only vaults
move with one command:

```bash
mkdir -p <vault_root>/matrix && mv <vault_root>/<each-room-folder> <vault_root>/matrix/
```

(`bot_type: "1on1"` flat layouts are unaffected.)

## Troubleshooting

Plugin not showing up? Hermes has verbose discovery logs:

```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

Common causes: the plugin isn't in `plugins.enabled`, or the gateway
wasn't restarted after install. See the
[Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin#debugging-plugin-discovery)
for the full debugging walkthrough.

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

## License

[MIT](LICENSE) — Copyright (c) 2026 Northbound.
