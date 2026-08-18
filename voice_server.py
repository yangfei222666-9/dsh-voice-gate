#!/usr/bin/env python3
# dsh-voice-gate:为 DeepSeek Harness 加一个"语音门"——手机一句话,直达你的 DSH 会话
# 零第三方依赖(Python 标准库);静态页 + /send 一键投递;可选 Token/PIN 鉴权
import json, os, secrets, sys, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.environ.get("VOICE_GATE_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
API = os.environ.get("DSH_API") or "http://127.0.0.1:3080"
PORT = int(os.environ.get("VOICE_GATE_PORT") or "3081")
BIND = os.environ.get("VOICE_GATE_BIND") or "127.0.0.1"
MAX_LEN = 2000

def _token() -> str:
    p = os.path.expanduser("~/.config/voice-gate.token")
    if os.path.exists(p):
        return open(p).read().strip()
    t = secrets.token_hex(16)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(t)
    os.chmod(p, 0o600)
    print(f"已生成令牌文件: {p}(手机端与页面自动使用,请勿外传)")
    return t

TOKEN = _token()

def api_call(method, rpc_id, payload):
    req = urllib.request.Request(
        f"{API}/api/{method}",
        data=json.dumps({"type": "client-request", "rpcId": rpc_id,
                         "method": method, "payload": payload}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def find_session():
    d = api_call("session.list", "voice-find", {})
    items = d["result"]["value"]["items"]
    live = [i for i in items if i.get("running")]
    live.sort(key=lambda i: -(i.get("projections", {}).get("values", {}).get("sessionStats", {}).get("turns", 0)))
    if live:
        return live[0]["sessionId"]
    alls = sorted(items, key=lambda i: -(i.get("projections", {}).get("values", {}).get("sessionStats", {}).get("turns", 0) or 0))
    return alls[0]["sessionId"] if alls else None

def send_text(text):
    sid = find_session()
    if not sid:
        return {"ok": False, "error": "没有可用的 DSH 会话"}
    api_call("session.prompt", "voice-send", {
        "sessionId": sid, "mode": "queue",
        "content": [{"type": "text", "text": text}]})
    return {"ok": True}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/send":
            q = urllib.parse.parse_qs(parsed.query)
            if (q.get("token") or [""])[0] != TOKEN:
                return self._json(403, {"ok": False, "error": "token 无效"})
            text = (q.get("text") or [""])[0].strip()
            if not text:
                return self._json(400, {"ok": False, "error": "text 为空"})
            if len(text) > MAX_LEN:
                return self._json(413, {"ok": False, "error": "太长"})
            try:
                return self._json(200, send_text(text))
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)[:120]})
        rel = urllib.parse.unquote(parsed.path).lstrip("/") or "index.html"
        path = os.path.realpath(os.path.join(ROOT, rel))
        if os.path.commonpath([path, ROOT]) != ROOT or not os.path.isfile(path):
            return self._json(404, {"ok": False, "error": "not found"})
        ctype = "text/html; charset=utf-8" if path.endswith(".html") else \
                "application/octet-stream" if path.endswith(".shortcut") else \
                "text/plain; charset=utf-8"
        data = open(path, "rb").read().decode("utf-8", "replace")
        if path.endswith(".html"):
            data = data.replace("__VOICE_TOKEN__", TOKEN)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data.encode())))
        self.end_headers()
        self.wfile.write(data.encode())

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

if __name__ == "__main__":
    HTTPServer((BIND, PORT), Handler).serve_forever()
