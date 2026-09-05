# dsh-voice-gate 🐳

> **痛点**:手机想给 DSH 发消息,打字麻烦、还要切网页。
> **解法**:语音门——按住说话→转文字→一条 `/send` 直达会话;本地文本桥接;时间/备份/股价等意图本地即答。
> **验证入口**:[离线测试](tests/test_voice_server.py)与 Python 3.9–3.13 CI；真实 DSH E2E 另行启用，见 [Development](#development)。

A **voice gate** for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH): speak a note on your phone, tap send, and it lands in your DSH session — through a locally running text gateway.

- 🎤 Mobile PWA page: hold-to-talk input (browser Web Speech API), text fallback, dark UI, whale icon
- 🔌 Zero third-party dependencies: Python standard library + a single-file front end
- 🔒 Token authentication (auto-generated, `0600`), path-traversal guard, 2000-char length cap
- 🧭 **Intent routing**: local answers (time/date, backup sentinel status, stock quotes) without touching a session; everything else is delivered to your DSH inbox
- 🔀 **`/api/*` reverse proxy** to your DSH web instance, so the bundled page works out of the box
- 🛰️ Loopback by default; phone access through Tailscale Serve or an equivalent private HTTPS reverse proxy
- 🐳 Installable PWA with a whale icon (add to iPhone home screen)

中文说明见 [README.zh-CN.md](README.zh-CN.md)。

[![CI](https://img.shields.io/github/actions/workflow/status/yangfei222666-9/dsh-voice-gate/ci.yml?branch=main)](https://github.com/yangfei222666-9/dsh-voice-gate/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Not a voice assistant and not an official DSH feature.** Speech-to-text happens in your phone's browser (Web Speech API) or your phone's keyboard dictation; this project serves a page, authenticates requests, and delivers text into a DSH session. It is a community project; it is not affiliated with, endorsed by, or part of DeepSeek. See [Claims and naming](#claims-and-naming).

## Architecture

```
Phone browser (Web Speech API → text)
  │  HTTPS via Tailscale Serve → loopback backend
  ▼
voice_server.py  (Python stdlib, http.server, port 3081, loopback; health :8899)
  │
  ├─ GET/POST /send ── token check (X-Voice-Token header; query credentials rejected)
  │      └─ intent router:
  │           ├─ time/date · backup sentinel · stock quotes → local answer (no DSH call)
  │           └─ everything else → session delivery:
  │                 session.list → pick: requested → saved → titled "手机语音门" → newest running
  │                 session.prompt (mode=queue, rpcId-tracked)
  │                 reply correlation: poll session.history, match rpcId → turn → answer
  │                 (no answer within 75s → background waiter, up to 600s, failure written back)
  ├─ GET /reply ── latest reply text (PWA polls this)
  ├─ GET /api/*, POST /api/* ── reverse proxy to DSH web (headers passed through, 20s timeout)
  └─ GET /* ── static page from www/ (__VOICE_TOKEN__ injected at serve time)
```

Failure contract (v0.4 audit): an upstream `ok:false` is **always** surfaced as an error (`502` from `/send`, or `{"ok": false}` in delivery results). The gate never pretends success.

## Install

Use Python 3.9+ on the DSH host. For phone access, connect the host and phone to the same Tailscale network, with access to this host allowed. Start DSH and create a session titled `手机语音门` (or your configured `VOICE_GATE_SESSION_TITLE`) before testing session delivery; a generic idle session is not selected by the running-session fallback.

```bash
# 1. Clone on the DSH host
git clone https://github.com/yangfei222666-9/dsh-voice-gate
cd dsh-voice-gate

# 2. Create a one-use six-digit pairing PIN before starting the gateway.
# This refuses to overwrite an existing PIN file. Keep the printed PIN private.
python3 - <<'PIN_SETUP'
import os, secrets
from pathlib import Path
pin_file = Path.home() / ".config" / "voice-gate.pin"
pin_file.parent.mkdir(mode=0o700, exist_ok=True)
pin = f"{secrets.randbelow(1_000_000):06d}"
fd = os.open(pin_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(pin)
print("One-use pairing PIN (keep private):", pin)
PIN_SETUP

# 3. Run and leave this terminal open.
python3 voice_server.py
# Defaults: listen 127.0.0.1:3081, health 127.0.0.1:8899, DSH API http://127.0.0.1:3080
```

The gateway creates its token on first start; it does **not** create a pairing PIN. If a PIN file already exists, use that PIN privately. A newly created PIN is loaded at startup; an already running gateway must be restarted to load it. Successful pairing consumes the PIN and removes its file.

In a second terminal on the host, configure the private HTTPS route:

```bash
# 4. Inspect existing routes, then mount this gateway at /voice.
tailscale serve status
tailscale serve --bg --https=443 --set-path=/voice http://127.0.0.1:3081
tailscale serve status
```

Keep an existing `/voice` route unless you intend to replace it. If prompted, follow Tailscale's HTTPS setup link. The command and prerequisites follow the official [Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve) and [Serve setup](https://tailscale.com/docs/features/tailscale-serve) documentation. `--bg` persists the route; this is a private tailnet route, not a public Funnel URL.

5. On the phone with Tailscale connected, open the HTTPS host shown by `tailscale serve status`, with `/voice/` appended, for example `https://your-host.your-tailnet.ts.net/voice/`. Enter the one-use PIN in the page's pairing field. The page exchanges it in a `POST /voice/auth` body and sends later requests with `X-Voice-Token`; never put either credential in a URL. First type `现在几点` and send: a local answer checks pairing and the gateway without invoking DSH. Then send a short message intended for the existing DSH session and check its reply or explicit pending/error state. Only after text works, try the microphone and optionally add the page to the home screen.

The default `127.0.0.1` listener cannot be reached directly at the computer's LAN IP. Keep that bind for this recipe; use the HTTPS `/voice/` URL on the phone. The bundled page sends requests to `/voice/*`, so the proxy route must include that prefix. If the page does not open, check both Tailscale connections, host access rules, the Serve route, and whether the gateway process is still running. A visible page alone does not prove DSH delivery.

### Optional macOS autostart

After the first connection works, stop the foreground gateway before enabling autostart to avoid two processes competing for port 3081. Replace the path below with your checkout path:

```bash
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
```

## Configuration

All configuration is environment-driven; nothing is hard-coded to a specific machine.

| Item | Env var | Default | Notes |
|---|---|---|---|
| Listen port | `VOICE_PORT` (alias `VOICE_GATE_PORT`) | `3081` | Loopback by default (`VOICE_GATE_BIND`) |
| DSH API base | `VOICE_GATE_DSH_API` (alias `DSH_API`) | `http://127.0.0.1:3080` | Used for `session.*` calls and `/api/*` proxy |
| Static root | `VOICE_GATE_ROOT` | `<repo>/www` | Page + `latest-reply.txt` |
| Health port | `VOICE_HEALTH_PORT` | `8899` | `GET /` → `{"ok":true,"svc":"voice-gate-health"}` |
| Token | — | `~/.config/voice-gate.token` | Auto-generated `secrets.token_hex(16)`, `0600` |
| Pairing PIN | — | `~/.config/voice-gate.pin` | Load before startup; one-use PIN exchanged via `POST /auth` body, then consumed; not a request-auth alternative |
| Voice session | `VOICE_GATE_SESSION_FILE` / `VOICE_GATE_SESSION_TITLE` / `VOICE_GATE_SESSION_PRESET` | `~/.config/voice-gate-session.json` / `手机语音门` / `standard` | Dedicated-session memory |
| Backup sentinel (optional) | `VOICE_GATE_OPS_DIR`, `VOICE_GATE_BACKUP_SENTINELS` | unset | `OPS_DIR`=ops root (for `logs/receipts.jsonl` + `logs/backup-failures.jsonl`); `SENTINELS`=`pathA;pathB` — the two backup copies' tail hashes are compared against each other (never against the live copy, which necessarily grows after each backup) |
| Stock quotes (optional) | `VOICE_GATE_PAPER_DIR`, `VOICE_GATE_STOCK_NAMES_JSON` | unset | `PAPER_DIR`=path to a [dsh-paper-trade](https://github.com/yangfei222666-9/dsh-paper-trade) checkout; `STOCK_NAMES_JSON`=path to a `{"名字": "SYM"}` map (self-configured; no personal watchlist is shipped) |

Unconfigured optional integrations degrade gracefully (e.g. "备份监控未配置" / no stock answers), never crash.

## API

### `GET /send` · `POST /send`

Auth: `X-Voice-Token: <token>` header. Any URL query containing `token` or `pin` (including empty or case-varied keys) is rejected with `400` before authentication, for both GET and POST. A pairing PIN is accepted only in the form body of `POST /auth`, which exchanges it once for a token; it cannot authenticate `/send` directly.

| Case | Response |
|---|---|
| Credential in a URL query | `400` `{"ok": false}`; use the header or pairing body |
| Bad or missing credentials | `403` `{"ok": false, "error": "token 无效"}` |
| Empty text | `400` `{"ok": false, "error": "text 为空"}` |
| Text longer than 2000 chars | `413` `{"ok": false, "error": "太长"}` |
| Local intent (time/backup/stock/status) | `200` `{"ok": true, "routed": "local", "answer": …}` |
| Delivered to a DSH session | `200` `{"ok": true, "routed": "voice-session", "sessionId": "<id>", "sessionSource": "requested|saved|voice-title|newest-running", "answer"/"pending"/"requestId": …}` |
| Session delivery failed (upstream `ok:false`, connection error, no session) | `502` (delivery failure) or `500` (session selection / upstream unreachable), always `"ok": false` with a short error — **never a fake success** |
| Specified `sessionId` does not exist | `500` `{"ok": false, "error": "指定的会话不存在: …"}` |

### Other endpoints

- `POST /auth` — one-use PIN exchange; form body `pin=…`, invalid/consumed PIN → `403`, repeated failures may return `429`.
- `GET /reply` — the latest reply text (the PWA polls this).
- `GET/POST /api/*` — reverse proxy to the DSH web instance: status and body passed through, original headers forwarded (hop-by-hop headers stripped), 20s timeout, upstream unreachable → `502`.
- `GET /` and other paths — static files from `www/`; `__VOICE_TOKEN__` is populated only for an already authenticated request; a first browser visit receives an empty placeholder and uses PIN pairing.

**Which session receives the note?** Selection order (each step falls through honestly): ① the `sessionId` you passed in the request → ② the saved dedicated voice session → ③ a session titled `手机语音门` (saved on first match) → ④ the newest *running* session by timestamp (multi-field fallback). The chosen source is reported in `sessionSource`; if no session matches, delivery fails explicitly — the gate never silently creates a session.

## Security

- Zero third-party dependencies (Python standard library only).
- Loopback-only listener by default; remote access is expected to go through Tailscale serve (private HTTPS) or an equivalent reverse proxy.
- Token auto-generated with `secrets.token_hex`, stored `0600` under `~/.config/`, injected only into already authenticated page requests, never committed.
- Path-traversal guard (`realpath` + `commonpath`) on static serving; 2000-char input cap.
- Reply files (`latest-reply.txt`, session file) are written atomically (temp + `os.replace`) with `0600`/`0700` modes.

## Known limitations

- `latest-reply.txt` holds the most recent reply only (last-write-wins per request id; older background waiters cannot overwrite a newer request's reply).
- The whale icon referenced by the page (`whale-icon.png`) is not included in this snapshot yet.
- Old URL credentials are rejected. Pair in the page, then use the token header; an old bookmark containing `token=` or `pin=` must be replaced with the plain HTTPS `/voice/` URL.

## FAQ

**Does it do speech recognition itself?** No. Use the HTTPS page in a browser that supports `SpeechRecognition`/`webkitSpeechRecognition`, allow microphone access, and start recognition with the page button. HTTPS alone does not guarantee API support; browser and standalone-PWA support vary. If recognition is unavailable or denied, type in the page or use the phone keyboard's 🎤 dictation key. The gateway receives text; browser recognition or keyboard dictation may use the device vendor's online service, so this project does not promise offline or fully on-device ASR. See [MDN SpeechRecognition](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition).

**What happens if DSH is not running?** Session delivery fails with `502`/`500` and an explicit error; nothing is queued or retried, and no fake success is ever returned. Local intents (time/backup/stock, when configured) keep working.

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
# Offline unit tests (Python 3.9+, standard library only; no DSH required)
python3 -m unittest discover -s tests -v

# Live E2E contract tests (boot the real server; E2E-2/3 use an in-process stub
# DSH, E2E-1/3 also hit your real DSH instance at VOICE_GATE_E2E_DSH_API):
VOICE_GATE_E2E=1 python3 -m unittest tests.test_e2e_live -v
```

CI runs the offline suite on Python 3.9–3.13 (`.github/workflows/ci.yml`); the E2E module is env-gated and never runs in CI.
