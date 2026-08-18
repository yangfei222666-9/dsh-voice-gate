# dsh-voice-gate 🐳

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)(DSH)装一扇**语音门**:手机一句话,直达你的 DSH 会话。

- 🎤 手机网页按住说话,自动转文字,一键发送
- 🔌 零第三方依赖:Python 标准库 + 单文件前端
- 🛰️ 配 Tailscale 可全球远程;不配也能局域网用
- 🔒 Token 鉴权(自动生成,0600 权限)+ 路径穿越防护 + 长度限制
- 🐳 主屏 PWA:加到 iPhone 主屏像 App 一样用(自带鲸鱼图标)

## 与 dsh-remote-web-ui 的差异

| | dsh-voice-gate(本仓库) | dsh-remote-web-ui |
|---|---|---|
| 形态 | **语音优先**,单页 | 完整界面镜像 |
| 网络 | Tailscale 全球通 / LAN 均可 | LAN |
| 安装 | 一个 Python 文件 + 一个页面 | 插件全家桶 |
| 适合 | 躺床上/开车/懒得打字 | 手机完整操作 |

## 5 步安装

```bash
# 1. 克隆
git clone https://github.com/yangfei222666-9/dsh-voice-gate
cd dsh-voice-gate

# 2. 启动(首次自动生成令牌 ~/.config/voice-gate.token)
python3 voice_server.py
# 默认监听 127.0.0.1:3081,环境变量可配:
#   VOICE_GATE_PORT / VOICE_GATE_BIND / DSH_API(默认 http://127.0.0.1:3080)

# 3. 开机自启(macOS launchd,把路径换成你的)
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

# 4. (可选,出门用)Tailscale 反向代理
tailscale serve --bg --set-path /voice http://127.0.0.1:3081

# 5. 手机:Safari 打开 http://<电脑IP>:3081/ → 分享 → 添加到主屏幕
```

## 原理

```
手机 Safari(语音识别,Web Speech API)
   │  HTTPS(Tailscale 私网)/ HTTP(LAN)
   ▼
voice_server.py :3081
   │  POST /api/session.prompt(官方 client-request 信封)
   ▼
DSH web :3080 —— 你的会话收到消息,agent 开始干活
```

`/send?token=...&text=...` 只做一件事:找到当前 DSH 会话,把文本投进去。

## 安全

- Token 自动生成,存 `~/.config/voice-gate.token`(0600),页面运行时注入,不落仓库
- 只监听 127.0.0.1(默认);公网暴露请走 Tailscale/HTTPS 反代
- 路径穿越防护(realpath/commonpath)+ 文本长度上限

## 已知边界

- iOS Safari 的 Web Speech API 在部分版本/独立 PWA 模式下不稳定;备用方案=页面输入框 + iPhone 键盘自带 🎤 听写键
- 只投递文本,不能读回 DSH 的回复(读回复请打开完整界面)

MIT License
