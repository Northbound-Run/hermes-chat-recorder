# Security Policy

hermes-chat-recorder writes the contents of your chats — across every connected
platform — to a Markdown vault, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/Northbound-Run/hermes-chat-recorder/security/advisories/new)
(Security → Report a vulnerability). Include the affected version, a description,
and reproduction steps if you have them. You'll get an acknowledgement, and a
fix or mitigation plan once the report is triaged.

Please give a reasonable window to address the issue before any public
disclosure.

## Supported versions

This project is pre-1.0; only the latest released version receives security
fixes.

| Version | Supported |
|---|---|
| 0.7.x   | ✅ |
| < 0.7   | ❌ |

## Security model

The design assumptions a reviewer should know:

- **The vault holds unredacted content** — every recorded message body, voice
  transcript, and image description lands in the vault as plain Markdown.
  Recording happens in a `pre_gateway_dispatch` hook, **before** Hermes's
  auth/pairing checks, so messages from *unpaired* senders are recorded too.
  **Treat `vault_root` as access-controlled storage**: put it on a path with
  appropriate filesystem permissions and back it up like sensitive data.
- **No third-party API keys** — the plugin has no credentials of its own. STT
  and vision are delegated to Hermes's built-in
  `tools.transcription_tools` / `tools.vision_tools`, which use whatever
  providers Hermes is already configured for. There are no tokens for this
  plugin to leak.
- **Recording-only** — the plugin never sends messages and never decides whether
  the agent wakes. It only reads dispatched events and writes the vault (plus
  rewriting `event.text` so a downstream wake sees the transcript/description).
  Use Hermes's native mention/allowlist settings to gate replies.
- **No inbound surface** — there is no webhook, public endpoint, or tunnel. The
  plugin only observes events the gateway already dispatches to it.
- **Untrusted message content** — recorded text comes from arbitrary remote
  senders. It is archived as data and must not be allowed to steer any LLM call
  the plugin makes (e.g. the vision/STT delegation paths).
- **Logs avoid PII** — the plugin logs through stdlib `logging` and does not log
  message contents or sender identifiers; the vault is the only place content is
  written.

If you find a gap in any of these — for example a path that logs message bodies,
or a way to write outside `vault_root` — that's exactly the kind of report we
want.
