#!/usr/bin/env python3
"""Live E2E contract tests for dsh-voice-gate (run explicitly, never in CI).

These tests boot the real voice_server against a real (or stubbed) DSH
instance and exercise the three contract paths from the v0.4 audit:

  E2E-1  GET/POST /send text bridge — local intent answered over real HTTP.
  E2E-2  fake-success → error — an upstream "ok:false" surfaces as an error,
         never as a fake 200.
  E2E-3  /api/* proxy — status/body pass through faithfully (stub 200 case +
         real-DSH fidelity case).

Gate: they only run when VOICE_GATE_E2E=1 is set. Without it, every test is
skipped, so the default unit-test suite and CI stay fully offline.

    VOICE_GATE_E2E=1 python3 -m unittest tests.test_e2e_live -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E = os.environ.get("VOICE_GATE_E2E") == "1"
REAL_DSH = os.environ.get("VOICE_GATE_E2E_DSH_API") or "http://127.0.0.1:3080"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(base, timeout=15):
    """Server is 'up' as soon as it answers anything on /reply (even an error
    status means the HTTP listener is serving)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(base + "/reply", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP response proves the listener is up
        except Exception:
            time.sleep(0.3)
    return False


class StubDSH(BaseHTTPRequestHandler):
    """Scriptable DSH stub: session.list returns one running session;
    session.prompt returns ok:false (fake failure) — both over POST /api/<method>."""
    log_message = lambda self, *a: None

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"ok": True, "svc": "stub"})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        method = body.get("method")
        if method == "session.list":
            self._json(200, {"result": {"ok": True, "value": {"items": [
                {"sessionId": "stub-s1", "running": True,
                 "projections": {"values": {"title": ""}},
                 "createdAt": 20260820090000}]}}})
        elif method == "session.prompt":
            # 审计修复①的契约:ok:false 必须被 voice-gate 如实上报
            self._json(200, {"result": {"ok": False, "error": "模拟 DSH 投递失败"}})
        else:
            self._json(200, {"result": {"ok": True, "value": {}}})

    def _json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def boot_voice(api_url, extra_env=None, root=None):
    """Boot voice_server.py as a subprocess; return (proc, base_url, token, up)."""
    port = free_port()
    health_port = free_port()
    tmp = tempfile.mkdtemp(prefix="voice-gate-e2e-")
    err_log = os.path.join(tmp, "voice-server.err.log")
    env = dict(os.environ)
    env.update({
        "VOICE_PORT": str(port),
        "VOICE_HEALTH_PORT": str(health_port),
        "VOICE_GATE_DSH_API": api_url,
        # Temp root: the E2E paths don't need the bundled page, and this keeps
        # runtime artifacts (latest-reply.txt) out of the repo.
        "VOICE_GATE_ROOT": root or tmp,
        "VOICE_GATE_SESSION_FILE": os.path.join(tmp, "session.json"),
        "HOME": tmp,
    })
    env.update(extra_env or {})
    with open(err_log, "wb") as errf:
        proc = subprocess.Popen([sys.executable, os.path.join(REPO_ROOT, "voice_server.py")],
                                env=env, stdout=subprocess.DEVNULL, stderr=errf)
    base = "http://127.0.0.1:%d" % port
    token_path = os.path.join(tmp, ".config", "voice-gate.token")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not os.path.exists(token_path):
        time.sleep(0.2)
    if os.path.exists(token_path):
        with open(token_path) as f:
            token = f.read().strip()
    else:
        token = ""
    self_check = bool(token) and wait_http(base)
    if not self_check:
        err_text = open(err_log, "rb").read().decode("utf-8", "replace")[-800:]
        raise RuntimeError("voice-gate failed to boot; stderr tail:\n%s" % err_text)
    return proc, base, token, self_check


@unittest.skipUnless(E2E, "set VOICE_GATE_E2E=1 to run live E2E contract tests")
class E2ETextBridge(unittest.TestCase):
    """E2E-1: GET/POST /send 文本桥(本地意图,不消耗 LLM,不触碰会话选择)。"""

    @classmethod
    def setUpClass(cls):
        cls.proc, cls.base, cls.token, cls.up = boot_voice(REAL_DSH)
        cls.assertTrue(cls.up, "voice-gate did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def test_post_send_local_intent_over_real_http(self):
        form = urllib.parse.urlencode({"text": "几点"}).encode()
        req = urllib.request.Request(self.base + "/send", data=form, method="POST",
                                     headers={"X-Voice-Token": self.token,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["routed"], "local")
        self.assertIn("answer", data)

    def test_get_send_query_token_deprecated_path(self):
        q = urllib.parse.urlencode({"token": self.token, "text": "在吗"})
        with urllib.request.urlopen(self.base + "/send?" + q, timeout=15) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["routed"], "local")


@unittest.skipUnless(E2E, "set VOICE_GATE_E2E=1 to run live E2E contract tests")
class E2EFakeSuccess(unittest.TestCase):
    """E2E-2: 上游 ok:false → voice-gate 必须返回错误(502),绝不假成功。"""

    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubDSH)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.api = "http://127.0.0.1:%d" % cls.stub.server_address[1]
        cls.proc, cls.base, cls.token, cls.up = boot_voice(cls.api)
        cls.assertTrue(cls.up, "voice-gate did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.stub.shutdown()
        cls.stub.server_close()

    def test_inbox_route_reports_upstream_failure(self):
        form = urllib.parse.urlencode({"text": "提醒 买牛奶"}).encode()
        req = urllib.request.Request(self.base + "/send", data=form, method="POST",
                                     headers={"X-Voice-Token": self.token,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 502)
        data = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("ok=false", data["error"])
        self.assertIn("模拟 DSH 投递失败", data["error"])


@unittest.skipUnless(E2E, "set VOICE_GATE_E2E=1 to run live E2E contract tests")
class E2EProxy(unittest.TestCase):
    """E2E-3: /api/* 反向代理——stub 200 透传 + 真实 DSH 保真透传(状态与 body 一致)。"""

    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubDSH)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.api = "http://127.0.0.1:%d" % cls.stub.server_address[1]
        cls.proc, cls.base, cls.token, cls.up = boot_voice(cls.api)
        cls.assertTrue(cls.up, "voice-gate did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.stub.shutdown()
        cls.stub.server_close()

    def test_proxy_stub_200_pass_through(self):
        """代理对 stub 200 响应的透传(状态+body)——'api 代理 200'契约。"""
        with urllib.request.urlopen(self.base + "/api/health", timeout=15) as r:
            self.assertEqual(r.status, 200)
            body = json.loads(r.read().decode("utf-8"))
        self.assertEqual(body, {"ok": True, "svc": "stub"})

    def test_proxy_real_dsh_fidelity(self):
        """代理对真实 DSH 的保真:上游状态/body == 代理状态/body。
        本用例专开一个接真实 DSH 的 voice-gate 实例(类级实例接的是 stub)。"""
        proc, base, token, up = boot_voice(REAL_DSH)
        try:
            path = "/api/health"
            try:
                urllib.request.urlopen(REAL_DSH + path, timeout=10)
            except urllib.error.HTTPError as e:
                upstream_status, upstream_body = e.code, e.read()
            else:
                upstream_status, upstream_body = 200, b""
            try:
                urllib.request.urlopen(base + path, timeout=15)
            except urllib.error.HTTPError as e:
                proxy_status, proxy_body = e.code, e.read()
            else:
                proxy_status, proxy_body = 200, b""
            self.assertEqual(proxy_status, upstream_status)
            self.assertEqual(proxy_body, upstream_body)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
