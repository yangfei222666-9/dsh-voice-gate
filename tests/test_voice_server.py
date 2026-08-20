#!/usr/bin/env python3
"""Tests for dsh-voice-gate v0.4 (standard-library unittest only, Python 3.9+).

Run from the repo root:
    python3 -m unittest discover -s tests -v

The module under test writes its token file to ~/.config/voice-gate.token at
import time, so HOME is pointed at a temp dir before importing it. All DSH
calls are mocked; nothing here touches a real DSH instance. Live E2E tests
against a real DSH instance live in tests/test_e2e_live.py (env-gated).
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Hermetic HOME: the token file lands in a temp dir, never the real ~/.config.
_TEST_HOME = tempfile.mkdtemp(prefix="voice-gate-test-home-")
os.environ["HOME"] = _TEST_HOME

# Import quietly: the module prints a token-file notice at import time.
_IMPORT_STDOUT = io.StringIO()
with contextlib.redirect_stdout(_IMPORT_STDOUT):
    import voice_server  # noqa: E402


def make_handler(path, method="GET"):
    """A Handler instance with a fake wfile; enough for do_GET/do_POST to run."""
    h = object.__new__(voice_server.Handler)
    h.path = path
    h.command = method
    h.headers = {}
    h.rfile = io.BytesIO()
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    h.requestline = "%s %s HTTP/1.0" % (method, path)
    h.request_version = "HTTP/1.0"
    return h


def run_get(path, headers=None):
    """Run do_GET for a path; return (status_code, parsed_body)."""
    h = make_handler(path)
    h.headers = headers or {}
    h.do_GET()
    raw = h.wfile.getvalue()
    _, sep, body = raw.rpartition(b"\r\n\r\n")
    status = int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP") else 0
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, body.decode("utf-8", "replace")


def run_post(path, form, headers=None):
    """Run do_POST with an x-www-form-urlencoded body; return (status, body)."""
    h = make_handler(path, method="POST")
    h.headers = headers or {}
    h.headers["Content-Length"] = str(len(form))
    h.rfile = io.BytesIO(form.encode("utf-8"))
    h.do_POST()
    raw = h.wfile.getvalue()
    _, sep, body = raw.rpartition(b"\r\n\r\n")
    status = int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP") else 0
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, body.decode("utf-8", "replace")


def session_list_value(items):
    return {"result": {"ok": True, "value": {"items": items}}}


def session_item(sid, running=True, title="", ts=0):
    item = {"sessionId": sid, "running": running,
            "projections": {"values": {"title": title}}}
    if ts:
        item["createdAt"] = ts
    return item


class TokenTest(unittest.TestCase):
    def test_token_file_created_with_0600(self):
        path = os.path.join(_TEST_HOME, ".config", "voice-gate.token")
        self.assertTrue(os.path.isfile(path), "token file should exist")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, "token file must be 0600")

    def test_token_is_32_hex_chars(self):
        self.assertRegex(voice_server.TOKEN, r"^[0-9a-f]{32}$")

    def test_pin_is_none_without_file(self):
        self.assertIsNone(voice_server.PIN)


class ApiValueTest(unittest.TestCase):
    """审计修复①:ok=false / 缺字段 必须报错,绝不假装成功。"""

    def test_ok_false_raises(self):
        with mock.patch.object(voice_server, "api_call",
                               return_value={"result": {"ok": False, "error": "模拟失败"}}):
            with self.assertRaises(RuntimeError) as ctx:
                voice_server.api_value("session.prompt", "r1", {})
        self.assertIn("ok=false", str(ctx.exception))
        self.assertIn("模拟失败", str(ctx.exception))

    def test_missing_result_raises(self):
        with mock.patch.object(voice_server, "api_call", return_value={"unexpected": 1}):
            with self.assertRaises(RuntimeError):
                voice_server.api_value("session.list", "r1", {})

    def test_missing_value_raises(self):
        with mock.patch.object(voice_server, "api_call",
                               return_value={"result": {"ok": True}}):
            with self.assertRaises(RuntimeError):
                voice_server.api_value("session.prompt", "r1", {})

    def test_value_returned_on_ok(self):
        with mock.patch.object(voice_server, "api_call",
                               return_value={"result": {"ok": True, "value": {"items": []}}}):
            self.assertEqual(voice_server.api_value("session.list", "r1", {}), {"items": []})


class SessionSelectionTest(unittest.TestCase):
    """审计修复②:选择顺序 requested → saved → voice-title → newest-running,并如实标注来源。"""

    def test_requested_id_selected(self):
        items = [session_item("s1", running=True, ts=1), session_item("s2", running=True, ts=9)]
        with mock.patch.object(voice_server, "_list_sessions", return_value=items):
            sid, source = voice_server.get_voice_session(requested_id="s1")
        self.assertEqual((sid, source), ("s1", "requested"))

    def test_unknown_requested_id_raises(self):
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s1")]):
            with self.assertRaises(RuntimeError) as ctx:
                voice_server.get_voice_session(requested_id="nope")
        self.assertIn("指定的会话不存在", str(ctx.exception))

    def test_saved_session_used_second(self):
        items = [session_item("s1"), session_item("s2", title="手机语音门")]
        with mock.patch.object(voice_server, "_list_sessions", return_value=items), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value="s1"):
            sid, source = voice_server.get_voice_session()
        self.assertEqual((sid, source), ("s1", "saved"))

    def test_title_match_used_third_and_saved(self):
        items = [session_item("s1", running=False), session_item("s2", title="手机语音门")]
        saved = {}
        with mock.patch.object(voice_server, "_list_sessions", return_value=items), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None), \
             mock.patch.object(voice_server, "_save_voice_session_id",
                               side_effect=lambda sid: saved.update(sid=sid)):
            sid, source = voice_server.get_voice_session()
        self.assertEqual((sid, source), ("s2", "voice-title"))
        self.assertEqual(saved.get("sid"), "s2")

    def test_newest_running_by_timestamp_fallback(self):
        items = [session_item("old", running=True, ts=20260801000000),
                 session_item("new", running=True, ts=20260820090000),
                 session_item("stopped", running=False, ts=99999999999999)]
        with mock.patch.object(voice_server, "_list_sessions", return_value=items), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None):
            sid, source = voice_server.get_voice_session()
        self.assertEqual((sid, source), ("new", "newest-running"))

    def test_timestamp_parse_falls_back_across_fields(self):
        # createdAt missing but updatedAt present -> still ordered correctly
        a = {"sessionId": "a", "running": True, "updatedAt": "2026-08-19T10:00:00Z"}
        b = {"sessionId": "b", "running": True, "updatedAt": "2026-08-20T10:00:00Z"}
        with mock.patch.object(voice_server, "_list_sessions", return_value=[a, b]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None):
            sid, source = voice_server.get_voice_session()
        self.assertEqual((sid, source), ("b", "newest-running"))

    def test_no_sessions_raises(self):
        with mock.patch.object(voice_server, "_list_sessions", return_value=[]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                voice_server.get_voice_session()
        self.assertIn("没有可用的 DSH 会话", str(ctx.exception))

    def test_find_session_returns_id(self):
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s9", running=True, ts=1)]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None):
            self.assertEqual(voice_server.find_session(), "s9")


class SendTextTest(unittest.TestCase):
    def _patch_wait(self, answer):
        return mock.patch.object(voice_server, "_wait_for_correlated_reply",
                                 return_value=(answer, 1, 1))

    def test_fake_success_reports_error(self):
        """审计修复①:E2E 级假成功——session.prompt 返回 ok=false 时绝不返回 ok:true。"""
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s1", running=True, ts=1)]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None), \
             mock.patch.object(voice_server, "api_value",
                               side_effect=RuntimeError("DSH API session.prompt 返回 ok=false: 模拟失败")):
            out = voice_server.send_text("hello")
        self.assertFalse(out["ok"])
        self.assertIn("ok=false", out["error"])
        self.assertEqual(out["sessionSource"], "newest-running")

    def test_send_returns_answer_when_correlated(self):
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s1", running=True, ts=1)]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None), \
             mock.patch.object(voice_server, "api_value", return_value={}), \
             self._patch_wait("好的,已办妥"):
            out = voice_server.send_text("hello")
        self.assertTrue(out["ok"])
        self.assertEqual(out["answer"], "好的,已办妥")

    def test_send_pending_when_no_reply_yet(self):
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s1", running=True, ts=1)]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None), \
             mock.patch.object(voice_server, "api_value", return_value={}), \
             self._patch_wait(None), \
             mock.patch.object(voice_server, "_finish_reply_later"):
            out = voice_server.send_text("hello", wait_timeout=0.01)
        self.assertTrue(out["ok"])
        self.assertTrue(out["pending"])
        self.assertIn("requestId", out)

    def test_no_sessions_propagates(self):
        with mock.patch.object(voice_server, "_list_sessions", return_value=[]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None):
            with self.assertRaises(RuntimeError):
                voice_server.send_text("hello")


class SendEndpointTest(unittest.TestCase):
    def test_post_header_token_accepted(self):
        status, body = run_post("/send", "text=" + urllib.parse.quote("几点"),
                                headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["routed"], "local")
        self.assertIn("answer", body)

    def test_post_without_token_403(self):
        status, body = run_post("/send", "text=几点")
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_post_wrong_token_403(self):
        status, body = run_post("/send", "text=几点",
                                headers={"X-Voice-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_get_query_token_deprecated_compat(self):
        q = "/send?" + urllib.parse.urlencode({"token": voice_server.TOKEN, "text": "几点"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, body = run_get(q)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("已弃用", err.getvalue())  # deprecation warning surfaced

    def test_get_wrong_token_403(self):
        status, body = run_get("/send?token=nope&text=几点")
        self.assertEqual(status, 403)

    def test_empty_text_400(self):
        status, body = run_post("/send", "text=   ",
                                headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 400)

    def test_too_long_text_413(self):
        status, body = run_post("/send", "text=" + "x" * 2001,
                                headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 413)

    def test_inbox_route_fake_success_returns_502(self):
        """审计修复①端点级:投递失败 → 502,绝不 200 假成功。"""
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s1", running=True, ts=1)]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None), \
             mock.patch.object(voice_server, "api_value",
                               side_effect=RuntimeError("DSH API session.prompt 返回 ok=false: 模拟失败")):
            status, body = run_post("/send", "text=" + urllib.parse.quote("提醒 买牛奶"),
                                    headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertIn("ok=false", body["error"])

    def test_inbox_route_ok_returns_200(self):
        with mock.patch.object(voice_server, "_list_sessions",
                               return_value=[session_item("s1", running=True, ts=1)]), \
             mock.patch.object(voice_server, "_load_voice_session_id", return_value=None), \
             mock.patch.object(voice_server, "api_value", return_value={}), \
             mock.patch.object(voice_server, "_wait_for_correlated_reply",
                               return_value=("收到", 1, 1)):
            status, body = run_post("/send", "text=" + urllib.parse.quote("提醒 买牛奶"),
                                    headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["routed"], "voice-session")
        self.assertEqual(body["answer"], "收到")

    def test_upstream_connection_error_500(self):
        with mock.patch.object(voice_server, "_list_sessions",
                               side_effect=urllib.error.URLError("connection refused")):
            status, body = run_post("/send", "text=" + urllib.parse.quote("提醒 买牛奶"),
                                    headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])

    def test_backup_intent_unconfigured_is_local_answer(self):
        status, body = run_post("/send", "text=" + urllib.parse.quote("备份状态"),
                                headers={"X-Voice-Token": voice_server.TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(body["routed"], "local")
        self.assertIn("未配置", body.get("answer", ""))


class StubUpstream(BaseHTTPRequestHandler):
    responses = []  # list of (path, status, body) consumed by tests

    def log_message(self, *a):
        pass

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        for path, status, body in StubUpstream.responses:
            if self.path == path:
                return self._reply(status, body)
        self._reply(404, b'{"ok":false,"error":"stub not found"}')


class ProxyTest(unittest.TestCase):
    """审计修复④:/api/* 反向代理——状态与 body 透传,上游不可达 502。"""

    def setUp(self):
        self.fqdn = mock.patch("socket.getfqdn", return_value="127.0.0.1")
        self.fqdn.start()
        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubUpstream)
        self.thread = threading.Thread(target=self.stub.serve_forever, daemon=True)
        self.thread.start()
        self.upstream = "http://127.0.0.1:%d" % self.stub.server_address[1]
        self.saved_api = voice_server.API
        voice_server.API = self.upstream

    def tearDown(self):
        voice_server.API = self.saved_api
        self.stub.shutdown()
        self.stub.server_close()
        self.fqdn.stop()

    def test_proxy_200_pass_through(self):
        StubUpstream.responses = [("/api/anything", 200, b'{"ok":true,"svc":"stub"}')]
        status, body = run_get("/api/anything")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "svc": "stub"})

    def test_proxy_upstream_404_pass_through(self):
        StubUpstream.responses = [("/api/missing", 404, b'{"ok":false}')]
        status, body = run_get("/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"ok": False})

    def test_proxy_upstream_down_502(self):
        voice_server.API = "http://127.0.0.1:1"
        status, body = run_get("/api/anything")
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertIn("代理失败", body["error"])


class StaticTest(unittest.TestCase):
    def test_index_served_with_token_injected(self):
        status, body = run_get("/")
        self.assertEqual(status, 200)
        self.assertIn(voice_server.TOKEN, body)
        self.assertNotIn("__VOICE_TOKEN__", body)

    def test_path_traversal_blocked(self):
        status, body = run_get("/../LICENSE")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_missing_file_returns_404(self):
        status, body = run_get("/no-such-file.html")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])


class IntegrationTest(unittest.TestCase):
    """Boot the real server on an ephemeral port and hit it over loopback."""

    def setUp(self):
        self.fqdn_patch = mock.patch("socket.getfqdn", return_value="127.0.0.1")
        self.fqdn_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), voice_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fqdn_patch.stop()

    def test_send_local_intent_over_http(self):
        form = urllib.parse.urlencode({"text": "几点"}).encode()
        req = urllib.request.Request(self.base + "/send", data=form, method="POST",
                                     headers={"X-Voice-Token": voice_server.TOKEN,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["routed"], "local")
        self.assertIn("answer", data)

    def test_bad_token_over_http_403(self):
        form = urllib.parse.urlencode({"text": "hi"}).encode()
        req = urllib.request.Request(self.base + "/send", data=form, method="POST",
                                     headers={"X-Voice-Token": "nope",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.read()


if __name__ == "__main__":
    unittest.main()
