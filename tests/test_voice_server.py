#!/usr/bin/env python3
"""Tests for dsh-voice-gate (standard-library unittest only, Python 3.9+).

Run from the repo root:
    python3 -m unittest discover -s tests -v

The module under test writes its token file to ~/.config/voice-gate.token at
import time, so HOME is pointed at a temp dir before importing it.
"""
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
import contextlib
from http.server import ThreadingHTTPServer
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


def make_handler(path):
    """A Handler instance with a fake wfile; enough for do_GET to run."""
    h = object.__new__(voice_server.Handler)
    h.path = path
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    h.requestline = "GET %s HTTP/1.0" % path
    h.request_version = "HTTP/1.0"
    return h


def run_get(path):
    """Run do_GET for a path; return (status_code, parsed_body)."""
    h = make_handler(path)
    h.do_GET()
    raw = h.wfile.getvalue()
    _, sep, body = raw.rpartition(b"\r\n\r\n")
    status = int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP") else 0
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, body.decode("utf-8", "replace")


def session_list_payload(items):
    return {"result": {"value": {"items": items}}}


class TokenTest(unittest.TestCase):
    def test_token_file_created_with_0600(self):
        path = os.path.join(_TEST_HOME, ".config", "voice-gate.token")
        self.assertTrue(os.path.isfile(path), "token file should exist")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, "token file must be 0600")

    def test_token_is_32_hex_chars(self):
        self.assertRegex(voice_server.TOKEN, r"^[0-9a-f]{32}$")


class SessionLogicTest(unittest.TestCase):
    def test_find_session_prefers_running_with_most_turns(self):
        items = [
            {"sessionId": "stopped-99", "running": False,
             "projections": {"values": {"sessionStats": {"turns": 99}}}},
            {"sessionId": "run-5", "running": True,
             "projections": {"values": {"sessionStats": {"turns": 5}}}},
            {"sessionId": "run-9", "running": True,
             "projections": {"values": {"sessionStats": {"turns": 9}}}},
        ]
        with mock.patch.object(voice_server, "api_call",
                               return_value=session_list_payload(items)):
            self.assertEqual(voice_server.find_session(), "run-9")

    def test_find_session_falls_back_to_most_active(self):
        items = [
            {"sessionId": "low", "running": False,
             "projections": {"values": {"sessionStats": {"turns": 2}}}},
            {"sessionId": "high", "running": False,
             "projections": {"values": {"sessionStats": {"turns": 42}}}},
        ]
        with mock.patch.object(voice_server, "api_call",
                               return_value=session_list_payload(items)):
            self.assertEqual(voice_server.find_session(), "high")

    def test_find_session_none_when_no_sessions(self):
        with mock.patch.object(voice_server, "api_call",
                               return_value=session_list_payload([])):
            self.assertIsNone(voice_server.find_session())

    def test_send_text_reports_error_without_sessions(self):
        with mock.patch.object(voice_server, "api_call",
                               return_value=session_list_payload([])):
            out = voice_server.send_text("hello")
        self.assertEqual(out, {"ok": False, "error": "没有可用的 DSH 会话"})

    def test_send_text_ok_sends_queue_prompt(self):
        items = [{"sessionId": "s1", "running": True,
                  "projections": {"values": {"sessionStats": {"turns": 1}}}}]
        responses = [session_list_payload(items), {"result": {"ok": True}}]
        with mock.patch.object(voice_server, "api_call",
                               side_effect=responses) as m:
            out = voice_server.send_text("买一瓶牛奶")
        self.assertEqual(out, {"ok": True})
        method, rpc_id, payload = m.call_args_list[1][0]
        self.assertEqual(method, "session.prompt")
        self.assertEqual(rpc_id, "voice-send")
        self.assertEqual(payload["sessionId"], "s1")
        self.assertEqual(payload["mode"], "queue")
        self.assertEqual(payload["content"], [{"type": "text", "text": "买一瓶牛奶"}])


class SendEndpointTest(unittest.TestCase):
    def _q(self, token, text):
        return "/send?" + urllib.parse.urlencode({"token": token, "text": text})

    def test_bad_token_returns_403(self):
        status, body = run_get(self._q("wrong-token", "hi"))
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_missing_token_returns_403(self):
        status, body = run_get("/send?text=hi")
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_empty_text_returns_400(self):
        status, body = run_get(self._q(voice_server.TOKEN, "   "))
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_too_long_text_returns_413(self):
        status, body = run_get(self._q(voice_server.TOKEN, "x" * 2001))
        self.assertEqual(status, 413)
        self.assertFalse(body["ok"])

    def test_ok_text_delivers_to_session(self):
        items = [{"sessionId": "s1", "running": True,
                  "projections": {"values": {"sessionStats": {"turns": 7}}}}]
        with mock.patch.object(voice_server, "api_call",
                               side_effect=[session_list_payload(items),
                                            {"result": {"ok": True}}]):
            status, body = run_get(self._q(voice_server.TOKEN, "hello world"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_upstream_failure_returns_500(self):
        with mock.patch.object(
                voice_server, "api_call",
                side_effect=urllib.error.URLError("connection refused")):
            status, body = run_get(self._q(voice_server.TOKEN, "hello"))
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])
        self.assertIn("connection refused", body["error"])


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
        self.saved_api_call = voice_server.api_call
        # TCPServer.__init__ calls socket.getfqdn(host), which does a reverse
        # DNS lookup; on some macOS setups the first (uncached) lookup stalls
        # for ~35s. Pin it to keep tests fast and hermetic everywhere.
        self.fqdn_patch = mock.patch("socket.getfqdn",
                                     return_value="127.0.0.1")
        self.fqdn_patch.start()
        self.calls = []

        def fake_api(method, rpc_id, payload):
            self.calls.append((method, rpc_id, payload))
            if method == "session.list":
                return session_list_payload(
                    [{"sessionId": "s1", "running": True,
                      "projections": {"values": {"sessionStats": {"turns": 3}}}}])
            return {"result": {"ok": True}}

        voice_server.api_call = fake_api
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), voice_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fqdn_patch.stop()
        voice_server.api_call = self.saved_api_call

    def test_index_served_over_http(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8", "replace")
        self.assertIn(voice_server.TOKEN, body)
        self.assertNotIn("__VOICE_TOKEN__", body)

    def test_send_roundtrip_over_http(self):
        q = urllib.parse.urlencode({"token": voice_server.TOKEN,
                                    "text": "hello from integration test"})
        with urllib.request.urlopen(self.base + "/send?" + q, timeout=10) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(any(m == "session.prompt" for m, _, _ in self.calls))

    def test_bad_token_over_http_returns_403(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                self.base + "/send?token=nope&text=hi", timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        # Drain the error body so the connection closes promptly
        # (avoids a lingering keep-alive read on the server thread).
        ctx.exception.read()


if __name__ == "__main__":
    unittest.main()
