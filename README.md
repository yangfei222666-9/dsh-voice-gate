# dsh-voice-gate 🐳

A **voice gate** for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH): speak a note on your phone, tap send, and it lands in your DSH session — locally, with no cloud relay.

- 🎤 Mobile PWA page: hold-to-talk input (browser Web Speech API), text fallback, dark UI, whale icon
- 🔌 Zero third-party dependencies: Python standard library + a single-file front end
- 🔒 Token authentication (auto-generated, `0600`), path-traversal guard, 2000-char length cap
- 🛰️ LAN out of the box; Tailscale (or any HTTPS reverse proxy) for remote use
- 🐳 Installable PWA with a whale icon (add to iPhone home screen)

中文说明见 [README.zh-CN.md](README.zh-CN.md)。

[![CI](https://img.shields.io/github/actions/workflow/status/yangfei222666-9/dsh-voice-gate/ci.yml?branch=main)](https://github.com/yangfei222666-9/dsh-voice-gate/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Not a voice assistant and not an official DSH feature.** Speech-to-text happens in your phone's browser (Web Speech API) or your phone's keyboard dictation; this project serves a page, authenticates requests, and delivers text into a DSH session. It is a community project; it is not affiliated with, endorsed by, or part of DeepSeek. See [Claims and naming](#claims-and-naming).

## How it works

```
Phone browser (Web Speech API → text)
  │  HTTPS via Tailscale serve, or HTTP on LAN
  ▼
voice_server.py  (Python stdlib, http.server, port 3081, loopback)
  │  GET /send?token=…&text=…
  │    session.list → pick the most active running session (highest turn count),
  │                     falling back to the most active session overall
  ▼  session.prompt (client-request envelope, mode=queue)
DSH web (127.0.0.1:3080)
```

## Install

```bash
# 1. Clone
git clone https://github.com/yangfei222666-9/dsh-voice-gate
cd dsh-voice-gate

# 2. Run (first start auto-generates ~/.config/voice-gate.token, mode 0600)
python3 voice_server.py
# Defaults: listen 127.0.0.1:3081, DSH API http://127.0.0.1:3080
# Environment: VOICE_GATE_PORT / VOICE_GATE_BIND / DSH_API

# 3. Autostart (macOS launchd; adjust the path)
cat > ~/Library/LaunchAgents/com.you.voice-gate.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.you.voice-gate</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>/ABSOLUTE/PATH/voice_server.py</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.you.voice-gate.plist

# 4. (Optional, for remote use) Tailscale HTTPS proxy
tailscale serve --bg --set-path /voice http://127.0.0.1:3081

# 5. Phone: Safari → http://<computer-IP>:3081/ → Share → Add to Home Screen
```

## Configuration

| Item | Default | Notes |
|---|---|---|
| Token | `~/.config/voice-gate.token` (auto-generated, `0600`) | Required for `/send` |
| Listen | `127.0.0.1:3081` (`VOICE_GATE_BIND` / `VOICE_GATE_PORT`) | Loopback only; expose via Tailscale/HTTPS reverse proxy, never directly to the internet |
| DSH API | `http://127.0.0.1:3080` (`DSH_API`) | Where `session.list` / `session.prompt` go |
| Max text length | 2000 chars | Longer input → HTTP 413 |

## API

`GET /send?token=…&text=…`

| Case | Response |
|---|---|
| Bad or missing token | `403` `{"ok": false, "error": "token 无效"}` |
| Empty text | `400` `{"ok": false, "error": "text 为空"}` |
| Text longer than 2000 chars | `413` `{"ok": false, "error": "太长"}` |
| Delivered to a DSH session | `200` `{"ok": true}` |
| No DSH session available | `200` `{"ok": false, "error": "没有可用的 DSH 会话"}` |
| Upstream (DSH) failure | `500` with a short error message |

`GET /` and other paths serve static files from `www/` (the HTML page gets the token injected at serve time; `__VOICE_TOKEN__` is never committed).

**Which session receives the note?** The most active *running* DSH session (highest turn count); if none is running, the most active session overall.

## Security

- Zero third-party dependencies (Python standard library only).
- Loopback-only listener by default; remote access is expected to go through Tailscale serve (private HTTPS) or an equivalent reverse proxy.
- Token auto-generated with `secrets.token_hex`, stored `0600` under `~/.config/`, injected into pages at serve time, never committed.
- Path-traversal guard (`realpath` + `commonpath`) on static serving.
- 2000-char input cap.

## Known limitations (this snapshot)

- The bundled `www` page issues DSH `client-request` envelopes to `/api/session.list` and `/api/session.prompt` on its own origin, while the bundled server implements the token-guarded `GET /send` bridge and static serving (it does not proxy `/api/*`). Use `/send` directly, or place the page behind a reverse proxy that forwards `/api/*` to DSH. A newer build that proxies `/api/*` server-side exists in the maintainer's deployment and will be published in a follow-up release.
- The whale icon referenced by the page (`whale-icon.png`) is not included in this snapshot yet.

## FAQ

**Does it do speech recognition itself?** No. Speech-to-text happens on your phone (browser Web Speech API or keyboard dictation). The server only receives text. Where in-app Web Speech is unavailable, fall back to the page's text input with the phone keyboard's 🎤 dictation key.

**What happens if DSH is not running?** `/send` returns `500` with a short error message; nothing is queued or retried.

**Is this an official DeepSeek product?** No. Community project, MIT licensed. See [Claims and naming](#claims-and-naming).

## Claims and naming (brand compliance)

Written to follow the official DSH [brand guidelines](https://github.com/deepseek-ai/deepseek-harness/blob/main/BRAND_GUIDELINES.md):

- The project name uses the **DSH** abbreviation, not the "DeepSeek Harness" trademark, as recommended by the guidelines.
- Descriptive text states the relationship factually ("a gate for DSH"); **no official affiliation, endorsement, partnership, or authorization is claimed anywhere**.
- Speech recognition is **not** claimed as an official DSH or DeepSeek capability — it is the device browser's feature.
- No performance/success claims beyond reproducible facts.
- LICENSE: MIT (see [LICENSE](LICENSE)).

## Development

```bash
# Run the test suite (Python 3.9+, standard library only)
python3 -m unittest discover -s tests -v
```

CI runs the suite on Python 3.9–3.13 (`.github/workflows/ci.yml`).
