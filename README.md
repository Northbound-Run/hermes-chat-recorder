# hermes-chat-recorder

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
records every Matrix chat message — text, voice notes, images — into an
Obsidian-style markdown vault.

> **Status: alpha (pre-1.0).** APIs and config schema may change. Used in
> production at Northbound.

## What it does

| Inbound | Behaviour |
|---|---|
| Text | Append the message to `vault_root/<room>/<YYYY-MM-DD>.md`. Pass through unmodified — Hermes's normal wake settings decide whether the agent replies. |
| Voice note | Transcribe via Hermes's built-in STT (`tools.transcription_tools.transcribe_audio`). Append placeholder + transcript section. Rewrite `event.text` to the transcript so the agent has usable content if it wakes. |
| Image | Describe via Hermes's built-in vision (`tools.vision_tools.vision_analyze_tool`). Append placeholder + description section. Rewrite `event.text` to caption + description + OCR'd text. |
| Reaction | Ignored (not recorded as a message section). |
| Outbound (the agent's own reply) | Recorded with a `reply_to:` link back to the trigger. |

This plugin is **recording-only**. It does NOT decide whether the agent
wakes up — use Hermes's native `MATRIX_REQUIRE_MENTION` /
`MATRIX_ALLOWED_USERS` settings for that.

**Pretty names.** Folder names use the room's `m.room.name` (or the DM
peer's display name for unnamed DMs), sanitized to filesystem-safe
slugs (e.g. `Matt-and-Annika/`). Section headers use the sender's
Matrix profile display name (with MXID-localpart fallback). All
resolutions are cached for the process lifetime; restart the gateway
to pick up a renamed room or profile.

**No third-party deps.** STT and vision are delegated to Hermes's
built-in tools. Whatever provider Hermes is configured for — local
faster-whisper, Groq, OpenAI, Mistral, xAI for STT; the main LLM or
the auxiliary vision provider for images — is what the recorder uses.
This package's only runtime requirement is Hermes itself.

## Install

```bash
pip install hermes-chat-recorder
```

Hermes's plugin loader auto-discovers entry-point packages via the
`hermes_agent.plugins` group — no extra wiring needed. Restart the
gateway and the plugin loads (assuming it's listed in `plugins.enabled`).

For development against an unreleased version, install editable:

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

    # Optional: manual name overrides. Useful when a Signal/WhatsApp
    # bridge user has no homeserver display name, or when you want to
    # rename a room. Overrides win over any automatic Matrix lookup.
    name_overrides:
      rooms:
        "!abcdef1234:agentchannels.dev": "Matt's Signal"
      users:
        "@signal_2c991545-...:agentchannels.dev": "Matt"
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

## Markdown vault format

Per-room, per-day file at `<vault_root>/<room-slug>/<YYYY-MM-DD>.md`,
sections separated by `---`, each anchored by an HTML comment:

```markdown
<!-- event:$abcd1234:server -->
### 09:14 Annika · voice (0:12) · stage:transcribed
**mxc:** mxc://server/abcdef
**duration_sec:** 12

> okay so the deck was titled "what ai can do for your business" and
> we showed it to the meridian team last thursday

---

<!-- event:$efgh5678:server -->
### 09:14 Ralph · reply · stage:sent
**reply_to:** $abcd1234:server

You bet — the AI-for-business one. I'll drop a refresher in your daily note.

---
```

Idempotency comes from the HTML-comment anchor — re-delivery of the
same Matrix `event_id` finds the existing section and either updates
its stage in-place or no-ops if already at a terminal stage.

## Architecture in one paragraph

A single Hermes plugin registers two integrations: (1) the
`pre_gateway_dispatch` hook for inbound messages, where it writes to
the vault, delegates voice transcription to Hermes's STT and image
description to Hermes's vision, and rewrites `event.text` to the
transcript/description; (2) a wrapped `send` on the live Matrix
adapter to capture outbound replies. Failure policy: per-event errors
are logged and written as `*_failed` stage sections;
startup/config/vault-write failures fail Hermes's readiness loudly.
See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

## License

[MIT](LICENSE) — Copyright (c) 2026 Northbound.
