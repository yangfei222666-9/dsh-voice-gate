# dsh-voice-gate 🐳

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)(DSH)装一扇**语音门**:手机一句话,直达你的 DSH 会话。

- 🎤 手机网页按住说话,自动转文字,一键发送
- 🔌 零第三方依赖:Python 标准库 + 单文件前端
- 🛰️ 默认只监听本机回环；手机通过 Tailscale Serve 私网 HTTPS 接入
- 🔒 Token 鉴权(自动生成,0600 权限)+ 路径穿越防护 + 长度限制
- 🐳 主屏 PWA:加到 iPhone 主屏像 App 一样用(自带鲸鱼图标)

## 与 dsh-remote-web-ui 的差异

| | dsh-voice-gate(本仓库) | dsh-remote-web-ui |
|---|---|---|
| 形态 | **语音优先**,单页 | 完整界面镜像 |
| 网络 | 本机回环 + Tailscale 私网 HTTPS | LAN |
| 安装 | 一个 Python 文件 + 一个页面 | 插件全家桶 |
| 适合 | 躺床上/开车/懒得打字 | 手机完整操作 |

## 安装与首次连接

电脑需 Python 3.9+。电脑和手机先接入同一 Tailscale 网络，并允许手机访问该电脑。测试会话投递前，先启动 DSH，并创建标题为“手机语音门”（或你配置的 `VOICE_GATE_SESSION_TITLE`）的会话；普通标题的空闲会话不会被“运行中会话”兜底选中。

```bash
# 1. 在 DSH 所在电脑克隆
git clone https://github.com/yangfei222666-9/dsh-voice-gate
cd dsh-voice-gate

# 2. 启动前创建一次性 6 位配对 PIN；已有文件时停止，不覆盖。
# 只在自己的终端查看这个 PIN，不分享终端截图。
python3 - <<'PIN_SETUP'
import os, secrets
from pathlib import Path
pin_file = Path.home() / ".config" / "voice-gate.pin"
pin_file.parent.mkdir(mode=0o700, exist_ok=True)
pin = f"{secrets.randbelow(1_000_000):06d}"
fd = os.open(pin_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(pin)
print("一次性配对 PIN（请保密）:", pin)
PIN_SETUP

# 3. 启动后保持此终端运行
python3 voice_server.py
# 默认监听 127.0.0.1:3081；DSH API 默认 http://127.0.0.1:3080。
```

服务器首次启动会生成 token，**不会自动生成配对 PIN**。若 PIN 文件已存在，请私下使用已有 PIN。新 PIN 只在启动时加载；已运行的服务器需重启后才能读取新文件。配对成功后，PIN 作废，其文件被删除。

在电脑的另一个终端配置私网 HTTPS：

```bash
# 4. 先检查已有路由，再挂载 /voice
tailscale serve status
tailscale serve --bg --https=443 --set-path=/voice http://127.0.0.1:3081
tailscale serve status
```

如果已有 `/voice` 路由，先确认是否要替换；其他服务的路由应保留。若命令提示启用 HTTPS，请按提示打开 Tailscale 配置页面。命令依据官方 [Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve) 与 [接入说明](https://tailscale.com/docs/features/tailscale-serve)；`--bg` 会持久保留路由，这里使用 tailnet 内的私网接入。

5. 手机保持 Tailscale 已连接，在浏览器打开 `tailscale serve status` 显示的 HTTPS 主机地址，加上 `/voice/`，例如 `https://your-host.your-tailnet.ts.net/voice/`。在页面的配对框输入一次性 PIN；页面通过 `POST /voice/auth` 正文兑换 token，后续请求使用 `X-Voice-Token` 请求头。不要把 PIN 或 token 放进网址。先输入并发送 `现在几点`，用本地回答检查配对与网关；再发一条给已有 DSH 会话的简短消息，检查回复或明确的 pending/错误状态。文字通路正常后再试麦克风，最后按需添加到主屏幕。

默认监听 `127.0.0.1`，手机不能直接访问 `http://电脑IP:3081/`。本说明保留回环监听，手机统一使用 HTTPS `/voice/` 地址；页面请求固定走 `/voice/*`，反代路由也需保留此前缀。网页打不开时，检查两端 Tailscale 连接、访问规则、Serve 路由和网关进程。页面能打开不等于 DSH 投递已经成功。

### 可选：macOS 开机自启

首次连接正常后，先停止前台网关，再启用自启，避免两个进程争用 3081 端口。把下面路径换成你的仓库路径：

```bash
cat > ~/Library/LaunchAgents/com.you.voice-gate.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.you.voice-gate</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>/绝对路径/voice_server.py</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.you.voice-gate.plist
```

## 原理

```
手机 Safari(语音识别,Web Speech API)
   │  HTTPS(Tailscale Serve 私网) → 本机回环
   ▼
voice_server.py :3081
   │  POST /api/session.prompt(官方 client-request 信封)
   ▼
DSH web :3080 —— 你的会话收到消息,agent 开始干活
```

`POST /voice/send` 用表单正文发送 `text`，鉴权使用 `X-Voice-Token` 请求头；服务器按意图本地回答或投递到 DSH 会话，并关联回复。任何 URL query 中的 `token` 或 `pin` 都返回 `400`，空值或大小写变体也不例外。PIN 只用于 `POST /voice/auth` 的表单正文，不能直接作为 `/send` 的鉴权。

## 安全

- Token 自动生成，存 `~/.config/voice-gate.token`（0600）；只有已鉴权的页面请求才注入 token。首次浏览器访问得到空占位，通过页面 PIN 配对后使用请求头。
- 默认只监听 `127.0.0.1`；手机接入使用 Tailscale Serve 私网 HTTPS 或同等受控反代。旧的含凭据书签应换成不含 query 的 HTTPS `/voice/` 地址。
- 路径穿越防护(realpath/commonpath)+ 文本长度上限

## 已知边界

- 页面内识别需要浏览器支持 `SpeechRecognition`/`webkitSpeechRecognition`、允许麦克风，并由用户点击按钮开始。使用 HTTPS 不保证浏览器或独立 PWA 支持该 API；不可用或权限被拒时，可直接输入，或用手机键盘的 🎤 听写键。
- 网关接收的是文字；浏览器识别或键盘听写可能使用厂商在线服务，不承诺离线或完全本机 ASR。参见 [MDN SpeechRecognition](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition)。
- 页面可显示关联回复或 pending/错误状态，但只保留最新回复；不替代完整 DSH 会话历史。

MIT License
