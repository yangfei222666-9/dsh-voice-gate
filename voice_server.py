#!/usr/bin/env python3
# dsh-voice-gate v0.4:为 DeepSeek Harness 加一个"语音门"——手机一句话,直达你的 DSH 会话
# 零第三方依赖(Python 标准库);静态页 + /send 一键投递 + 常听意图路由 + /api/* 反向代理
# v0.4 审计修复(2026-08-21):①假成功(ok=false 必报错)②会话选择多字段时间戳+如实标注 sessionSource
#   ③鉴权只接受 X-Voice-Token header,配对 PIN 只接受 /auth body ④/api/* 代理(GET+POST,20s 超时)
# 配置全部走环境变量(见 README):VOICE_GATE_ROOT / VOICE_PORT / VOICE_GATE_DSH_API / VOICE_GATE_BIND /
#   VOICE_GATE_OPS_DIR / VOICE_GATE_BACKUP_SENTINELS / VOICE_GATE_PAPER_DIR / VOICE_GATE_STOCK_NAMES_JSON /
#   VOICE_HEALTH_PORT / VOICE_GATE_SESSION_FILE / VOICE_GATE_SESSION_TITLE / VOICE_GATE_SESSION_PRESET
import json, os, sys, time, hashlib, threading, urllib.request, urllib.parse, uuid, secrets, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("VOICE_GATE_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
PORT = int(os.environ.get("VOICE_PORT") or os.environ.get("VOICE_GATE_PORT") or "3081")
BIND = os.environ.get("VOICE_GATE_BIND") or "127.0.0.1"
API = os.environ.get("VOICE_GATE_DSH_API") or os.environ.get("DSH_API") or "http://127.0.0.1:3080"
HEALTH_PORT = int(os.environ.get("VOICE_HEALTH_PORT") or "8899")
MAX_LEN = 2000
MAX_FORM_BODY = 32 * 1024
MAX_API_BODY = 1024 * 1024
BODY_READ_TIMEOUT = 5
PAIR_MAX_FAILURES = 5
PAIR_FAILURE_WINDOW = 60
PAIR_LOCK_SECONDS = 300
PAIR_MAX_SOURCES = 1024

def _secret_file(name, auto_generate=True):
    path = os.path.expanduser(os.path.join("~/.config", name))
    if os.path.exists(path):
        return open(path).read().strip()
    if not auto_generate:
        return None
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    value = secrets.token_hex(16)
    # TOCTOU 修复(七视角复审 8-23):os.open 直接以 0600 创建,消除"先写后 chmod"的窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    print("已生成令牌文件: {} (0600,请勿外传)".format(path))
    return value

TOKEN = _secret_file("voice-gate.token")
PIN = _secret_file("voice-gate.pin", auto_generate=False)
_PAIR_FAILURES = {}
_PAIR_FAILURES_LOCK = threading.Lock()

def _secret_matches(candidate, configured):
    """只比较非空字符串凭据,避免空配置与缺失 header 等值通过。"""
    if not isinstance(candidate, str) or not isinstance(configured, str):
        return False
    if not candidate or not configured:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8"))

def _token_matches(candidate):
    return _secret_matches(candidate, TOKEN)

def _prune_pair_failures(now):
    stale_after = max(PAIR_FAILURE_WINDOW, PAIR_LOCK_SECONDS)
    for source, state in list(_PAIR_FAILURES.items()):
        if state["locked_until"] <= now and now - state["last_seen"] > stale_after:
            _PAIR_FAILURES.pop(source, None)

def _consume_pairing_pin(source, candidate, now=None):
    """原子校验一次性 PIN;按 TCP peer 统计失败窗口并锁定来源。"""
    global PIN
    now = time.monotonic() if now is None else now
    source = source or "<unknown>"
    with _PAIR_FAILURES_LOCK:
        _prune_pair_failures(now)
        state = _PAIR_FAILURES.get(source)
        if state and state["locked_until"] > now:
            retry = max(1, int(state["locked_until"] - now + 0.999))
            return "locked", retry

        if _secret_matches(candidate, PIN):
            PIN = None
            _PAIR_FAILURES.pop(source, None)
            return "ok", 0

        if state is None:
            if len(_PAIR_FAILURES) >= PAIR_MAX_SOURCES:
                oldest = min(_PAIR_FAILURES, key=lambda key: _PAIR_FAILURES[key]["last_seen"])
                _PAIR_FAILURES.pop(oldest, None)
            state = {"failures": [], "locked_until": 0.0, "last_seen": now}
            _PAIR_FAILURES[source] = state

        recent = [ts for ts in state["failures"] if now - ts <= PAIR_FAILURE_WINDOW]
        recent.append(now)
        state["failures"] = recent
        state["last_seen"] = now
        if len(recent) >= PAIR_MAX_FAILURES:
            state["failures"] = []
            state["locked_until"] = now + PAIR_LOCK_SECONDS
            return "locked", PAIR_LOCK_SECONDS
        return "invalid", 0

def _restore_pairing_pin(candidate):
    """持久作废失败时恢复内存 PIN;期间不会向任何请求发放 token。"""
    global PIN
    with _PAIR_FAILURES_LOCK:
        if PIN is None:
            PIN = candidate

VOICE_SESSION_FILE = os.environ.get("VOICE_GATE_SESSION_FILE") or os.path.expanduser("~/.config/voice-gate-session.json")
VOICE_SESSION_TITLE = os.environ.get("VOICE_GATE_SESSION_TITLE") or "手机语音门"
VOICE_SESSION_PRESET = os.environ.get("VOICE_GATE_SESSION_PRESET") or "standard"
VOICE_REPLY_TIMEOUT = 75
VOICE_BACKGROUND_TIMEOUT = 600
REPLY_WRITE_LOCK = threading.Lock()
LATEST_REQUEST_ID = None

# ── 可选的本地集成(未配置则对应能力自动禁用,见 README)────────────────────────
# ① 备份哨兵监控:VOICE_GATE_OPS_DIR=管家 ops 根目录;VOICE_GATE_BACKUP_SENTINELS="副本A路径;副本B路径"
OPS = os.environ.get("VOICE_GATE_OPS_DIR") or ""
SENTINELS = [s for s in (os.environ.get("VOICE_GATE_BACKUP_SENTINELS") or "").split(";") if s.strip()]
# ② 股票行情:VOICE_GATE_PAPER_DIR 指向 dsh-paper-trade 检出目录;
#    VOICE_GATE_STOCK_NAMES_JSON 指向 {"中文名": "SYM"} 映射文件(示例注释,默认空)
PAPER_DIR = os.environ.get("VOICE_GATE_PAPER_DIR") or ""
STOCK_NAMES = {}
# STOCK_NAMES 示例(自配):{"苹果": "AAPL", "微软": "MSFT", "英伟达": "NVDA"}
_stock_json = os.environ.get("VOICE_GATE_STOCK_NAMES_JSON") or ""
if _stock_json and os.path.exists(_stock_json):
    try:
        STOCK_NAMES.update(json.load(open(_stock_json)))
    except Exception:
        pass

def stock_quote(text):
    """从文本里找股票名并返回最新行情;未配置 PAPER_DIR 或找不到返回 None"""
    if not PAPER_DIR:
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("prices", os.path.join(PAPER_DIR, "prices.py"))
    prices = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prices)
    for name, sym in STOCK_NAMES.items():
        if name in text:
            px, date, _ = prices.latest(sym)
            if px is None:
                return "{} 暂时拉不到行情,稍后再试 🐳".format(name)
            if sym.endswith(".KS"):
                return "{}({}) 最新收盘 {:,.0f} 韩元 · {}".format(name, sym, px, date)
            return "{}({}) 最新收盘 ${:,.2f} · {}".format(name, sym, px, date)
    for word in text.split():
        w = word.strip("。,!?,.?").upper()
        if w and (w.endswith(".KS") or (w.isalpha() and len(w) <= 5)):
            spec = importlib.util.spec_from_file_location("prices", os.path.join(PAPER_DIR, "prices.py"))
            prices = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(prices)
            px, date, _ = prices.latest(w)
            if px is not None:
                return "{} 最新收盘 ${:,.2f} · {}".format(w, px, date)
    return None

def api_call(method, rpc_id, payload, timeout=20):
    req = urllib.request.Request(
        f"{API}/api/{method}",
        data=json.dumps({"type": "client-request", "rpcId": rpc_id,
                         "method": method, "payload": payload}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def api_value(method, rpc_id, payload, timeout=20):
    """DSH API 调用并解包 value;审计修复:显式检查 result.ok,ok=false 必须报错,绝不假装成功"""
    data = api_call(method, rpc_id, payload, timeout=timeout)
    try:
        result = data["result"]
    except (KeyError, TypeError):
        raise RuntimeError("DSH API {} 返回错误(缺 result)".format(method))
    if result.get("ok") is False:
        err = str(result.get("error") or "ok=false")[:120]
        raise RuntimeError("DSH API {} 返回 ok=false: {}".format(method, err))
    try:
        return result["value"]
    except (KeyError, TypeError):
        raise RuntimeError("DSH API {} 返回错误(缺 value)".format(method))

def _session_title(item):
    return item.get("projections", {}).get("values", {}).get("title", "")

def _session_stamp(item):
    """会话时间戳(用于选最新):多字段兜底,任一可解析即用;全部缺失返回 0(按列表序)"""
    for k in ("createdAt", "created_at", "updatedAt", "updated_at", "ts", "lastActiveAt"):
        v = item.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            try:
                return int(v.replace("-", "").replace(":", "").replace("T", "").replace("Z", "").replace(" ", "").replace(".", ""))
            except Exception:
                continue
    return 0

def _list_sessions():
    return api_value("session.list", "voice-list-{}".format(time.time_ns()), {}).get("items", [])

def _load_voice_session_id():
    try:
        with open(VOICE_SESSION_FILE) as f:
            value = json.load(f)
        return value.get("sessionId") if isinstance(value, dict) else None
    except Exception:
        return None

def _save_voice_session_id(session_id):
    directory = os.path.dirname(VOICE_SESSION_FILE)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temp_path = VOICE_SESSION_FILE + ".tmp"
    with open(temp_path, "w") as f:
        json.dump({"sessionId": session_id}, f)
        f.write("\n")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, VOICE_SESSION_FILE)

def get_voice_session(requested_id=None):
    """审计修复:不依赖不存在的 sessionStats.turns 字段。
    选择顺序:①调用方指定 sessionId ②已保存的专用语音会话 ③标题=手机语音门 ④最新 running 会话。
    返回 (session_id, source),source ∈ requested/saved/voice-title/newest-running。"""
    items = _list_sessions()
    by_id = {item.get("sessionId"): item for item in items if item.get("sessionId")}
    if requested_id:
        if requested_id in by_id:
            return requested_id, "requested"
        raise RuntimeError("指定的会话不存在: {}".format(requested_id[:32]))
    stored = _load_voice_session_id()
    if stored and stored in by_id:
        return stored, "saved"
    for item in items:
        if _session_title(item) == VOICE_SESSION_TITLE:
            session_id = item.get("sessionId")
            if session_id:
                _save_voice_session_id(session_id)
                return session_id, "voice-title"
    running = [i for i in items if i.get("running")]
    if running:
        running.sort(key=_session_stamp, reverse=True)
        sid = running[0].get("sessionId")
        if sid:
            return sid, "newest-running"
    raise RuntimeError("没有可用的 DSH 会话")

def find_session(requested_id=None):
    return get_voice_session(requested_id)[0]

def _history_page(session_id, max_messages=12, before_seq=None, timeout=5):
    payload = {"sessionId": session_id, "maxMessages": max_messages}
    if before_seq is not None:
        payload["beforeSeq"] = before_seq
    return api_value("session.history", "voice-history-{}".format(time.time_ns()),
                     payload, timeout=timeout)

def _history_events(session_id, max_messages=3, timeout=5):
    value = _history_page(session_id, max_messages=max_messages, timeout=timeout)
    return [entry.get("event", entry) for entry in value.get("events", [])]

def _history_window_for_rpc(session_id, prompt_rpc_id, timeout=5, max_pages=12):
    collected = {}
    before_seq = None
    for _ in range(max_pages):
        page = _history_page(session_id, max_messages=12,
                             before_seq=before_seq, timeout=timeout)
        events = [entry.get("event", entry) for entry in page.get("events", [])]
        for event in events:
            seq = event.get("seq")
            if isinstance(seq, int):
                collected[seq] = event
        found = any(event.get("type") == "user/message"
                    and event.get("data", {}).get("source", {}).get("rpcId") == prompt_rpc_id
                    for event in events)
        if found or not page.get("hasMore"):
            break
        seqs = [event.get("seq") for event in events if isinstance(event.get("seq"), int)]
        if not seqs:
            break
        next_before = min(seqs)
        if before_seq == next_before:
            break
        before_seq = next_before
    return [collected[seq] for seq in sorted(collected)]

def _last_event_seq(events):
    return max([event.get("seq", -1) for event in events] or [-1])

def _update_reply_correlation(events, prompt_rpc_id, user_seq=None, turn=None):
    for event in events:
        if user_seq is not None or event.get("type") != "user/message":
            continue
        source = event.get("data", {}).get("source", {})
        if isinstance(source, dict) and source.get("rpcId") == prompt_rpc_id:
            user_seq = event.get("seq", -1)
            break
    if user_seq is not None and turn is None:
        for event in events:
            if event.get("seq", -1) <= user_seq or event.get("type") != "step/start":
                continue
            turn = event.get("data", {}).get("turn")
            if turn is not None:
                break
    return user_seq, turn

def _assistant_text_for_turn(events, turn):
    if turn is None:
        return None
    if not any(event.get("type") == "turn/end"
               and event.get("data", {}).get("turn") == turn for event in events):
        return None
    answer = None
    for event in events:
        if event.get("type") != "assistant/message" or event.get("data", {}).get("turn") != turn:
            continue
        message = event.get("data", {}).get("message", {})
        parts = message.get("content", []) if isinstance(message, dict) else []
        text = "\n".join(part.get("text", "") for part in parts
                         if isinstance(part, dict) and part.get("type") == "text").strip()
        if text:
            answer = text
    return answer

def _assistant_text_for_rpc(events, prompt_rpc_id):
    user_seq, turn = _update_reply_correlation(events, prompt_rpc_id)
    return _assistant_text_for_turn(events, turn)

def _write_latest_reply(text, request_id=None):
    target = os.path.join(ROOT, "latest-reply.txt")
    temp_path = target + ".{}.tmp".format(uuid.uuid4().hex)
    with REPLY_WRITE_LOCK:
        if request_id is not None and request_id != LATEST_REQUEST_ID:
            return False
        try:
            with open(temp_path, "w") as f:
                f.write(text)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, target)
            return True
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                pass

def _secure_reply_file():
    target = os.path.join(ROOT, "latest-reply.txt")
    if not os.path.exists(target):
        _write_latest_reply("(管家还没写回复)")
    else:
        os.chmod(target, 0o600)

def _wait_for_correlated_reply(session_id, prompt_rpc_id, wait_timeout,
                               user_seq=None, turn=None):
    deadline = time.monotonic() + max(1, wait_timeout)
    while time.monotonic() < deadline:
        remaining = max(1, deadline - time.monotonic())
        try:
            events = _history_events(session_id, 12, timeout=min(5, remaining))
            user_seq, turn = _update_reply_correlation(
                events, prompt_rpc_id, user_seq=user_seq, turn=turn)
            if user_seq is None or turn is None:
                window = _history_window_for_rpc(
                    session_id, prompt_rpc_id, timeout=min(5, remaining))
                user_seq, turn = _update_reply_correlation(
                    window, prompt_rpc_id, user_seq=user_seq, turn=turn)
            answer = _assistant_text_for_turn(events, turn)
            if answer:
                return answer, user_seq, turn
        except Exception:
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return None, user_seq, turn

def _finish_reply_later(session_id, prompt_rpc_id, user_seq=None, turn=None):
    answer, _, _ = _wait_for_correlated_reply(
        session_id, prompt_rpc_id, VOICE_BACKGROUND_TIMEOUT,
        user_seq=user_seq, turn=turn)
    if answer:
        _write_latest_reply(answer, prompt_rpc_id)
    else:
        # 审计修复:后台等待超时也要有失败通道,不能静默
        _write_latest_reply("(管家在后台等待窗口内没有回复)", prompt_rpc_id)

def send_text(text, wait_timeout=VOICE_REPLY_TIMEOUT, requested_id=None):
    global LATEST_REQUEST_ID
    session_id, source = get_voice_session(requested_id)
    prompt_rpc_id = "voice-send-{}".format(uuid.uuid4().hex)
    LATEST_REQUEST_ID = prompt_rpc_id
    _write_latest_reply("(管家还没写回复)", prompt_rpc_id)
    try:
        api_value("session.prompt", prompt_rpc_id, {
            "sessionId": session_id,
            "mode": "queue",
            "content": [{"type": "text", "text": text}],
        })
    except Exception as e:
        # 审计修复:投递失败必须返回 ok=false,绝不假装成功
        msg = str(e)[:120]
        _write_latest_reply("(投递失败: {})".format(msg[:100]), prompt_rpc_id)
        return {"ok": False, "error": msg, "sessionId": session_id, "sessionSource": source}
    answer, user_seq, turn = _wait_for_correlated_reply(
        session_id, prompt_rpc_id, wait_timeout)
    if answer:
        _write_latest_reply(answer, prompt_rpc_id)
        return {"ok": True, "sessionId": session_id, "sessionSource": source, "answer": answer}
    threading.Thread(target=_finish_reply_later,
                     args=(session_id, prompt_rpc_id, user_seq, turn), daemon=True).start()
    return {"ok": True, "sessionId": session_id, "sessionSource": source,
            "requestId": prompt_rpc_id, "pending": True}

def _tail2000(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - 2000))
            return f.read()
    except Exception:
        return None

def backup_status():
    """哨兵比对:两副本 receipts.jsonl 尾行哈希互比(本地账本备份后必然增长,不以本机为基准)。
    未配置 VOICE_GATE_OPS_DIR/VOICE_GATE_BACKUP_SENTINELS 时返回未配置提示。"""
    if not OPS or len(SENTINELS) < 2:
        return "备份监控未配置(设置 VOICE_GATE_OPS_DIR 与 VOICE_GATE_BACKUP_SENTINELS)"
    today = time.strftime("%Y-%m-%d")
    fails_today = 0
    last_fail = ""
    fail_log = os.path.join(OPS, "logs", "backup-failures.jsonl")
    try:
        for line in open(fail_log):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ts", "").startswith(today):
                fails_today += 1
                last_fail = d.get("err", "")[:60]
    except Exception:
        pass
    tails = [_tail2000(p) for p in SENTINELS[:2]]
    if any(tail is None or not tail for tail in tails):
        sentinel_state = " BLOCKED/unknown"
    else:
        hashes = [hashlib.sha1(tail).hexdigest()[:12] for tail in tails]
        sentinel_state = "一致 ✅" if hashes[0] == hashes[1] else "不一致 ⚠️"
    line = "备份:两副本哨兵{};今日失败 {} 段".format(sentinel_state, fails_today)
    if fails_today and last_fail:
        line += "({})".format(last_fail)
    return line

def now_text():
    return "现在是 " + time.strftime("%Y-%m-%d %H:%M") + "(本机时间)"

def route_intent(text):
    """返回 (answer, forward):answer 为空则代表无本地答复;forward 为 None 则不投递收件箱"""
    t = text.lower()
    if any(k in t for k in ["备份", "backup", "哨兵"]):
        return backup_status(), None
    if any(k in t for k in ["几点", "时间", "日期", "今天几号", "现在时间", "time", "what time"]):
        return now_text(), None
    if any(k in t for k in ["股价", "行情", "多少钱", "收盘", "查一下", "查查"]):
        q = stock_quote(t)
        if q:
            return q, None
    status_text = t.strip().rstrip("。！？!?").strip()
    status_subjects = ("状态", "当前状态", "现在状态", "管家状态", "服务状态")
    status_intents = {
        "在吗", "你在吗", "管家在吗", "还在吗", "你还在吗", "管家还在吗",
        "在线吗", "你在线吗", "管家在线吗",
        "活着吗", "你活着吗", "管家活着吗", "alive", "are you alive",
        "现在什么状态", "现在是什么状态", "当前是什么状态",
    }
    status_intents.update(status_subjects)
    status_intents.update(prefix + subject
                          for prefix in ("查", "查询", "查看", "检查", "请查", "请查询", "请查看", "请检查")
                          for subject in status_subjects)
    status_intents.update(subject + suffix
                          for subject in status_subjects
                          for suffix in ("如何", "怎么样", "正常吗", "是否正常"))
    if status_text in status_intents:
        return "我在,管家在线 🐳 " + now_text() + ";" + backup_status(), None
    if t.startswith(("提醒", "记得", "别忘了", "待办")):
        return "已记下并转给管家 📝:" + text[:50], "【提醒】" + text
    return "", "【语音】" + text

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @staticmethod
    def _normalized_path(path):
        if path.startswith("/voice"):
            return "/" + path[len("/voice"):].lstrip("/")
        return path

    def _reject_query_credentials(self, parsed):
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if any(key.lower() in ("token", "pin") for key in query):
            self._json(400, {"ok": False, "error":
                       "查询参数禁止携带凭据;token 请用 X-Voice-Token header,PIN 请用 POST /auth body"})
            return True
        return False

    def _check_auth(self):
        """已配对请求只接受非空 X-Voice-Token header。"""
        return _token_matches(self.headers.get("X-Voice-Token"))

    def _validated_content_length(self, limit):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json(400, {"ok": False, "error": "不支持 Transfer-Encoding"})
            return None
        if hasattr(self.headers, "get_all"):
            length_headers = self.headers.get_all("Content-Length") or []
            if len(length_headers) > 1:
                self.close_connection = True
                self._json(400, {"ok": False, "error": "Content-Length 重复"})
                return None
            raw = length_headers[0] if length_headers else None
        else:
            raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        if "," in str(raw):
            self.close_connection = True
            self._json(400, {"ok": False, "error": "Content-Length 无效"})
            return None
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self.close_connection = True
            self._json(400, {"ok": False, "error": "Content-Length 无效"})
            return None
        if length < 0:
            self.close_connection = True
            self._json(400, {"ok": False, "error": "Content-Length 无效"})
            return None
        if length > limit:
            self.close_connection = True
            self._json(413, {"ok": False, "error": "请求正文过大"})
            return None
        return length

    def _read_body(self, length):
        if length == 0:
            return b""
        connection = getattr(self, "connection", None)
        old_timeout = None
        timeout_set = False
        if connection is not None and hasattr(connection, "settimeout"):
            try:
                old_timeout = connection.gettimeout()
                timeout_set = True
            except OSError:
                self.close_connection = True
                self._json(500, {"ok": False, "error": "无法设置正文读取超时"})
                return None
        chunks = []
        remaining = length
        deadline = time.monotonic() + BODY_READ_TIMEOUT
        read_error = None
        try:
            while remaining:
                seconds_left = deadline - time.monotonic()
                if seconds_left <= 0:
                    read_error = "timeout"
                    break
                if timeout_set:
                    connection.settimeout(max(0.001, seconds_left))
                reader = getattr(self.rfile, "read1", self.rfile.read)
                chunk = reader(min(64 * 1024, remaining))
                if not chunk:
                    read_error = "short"
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except (socket.timeout, TimeoutError):
            read_error = "timeout"
        except OSError:
            read_error = "io"
        finally:
            if timeout_set:
                try:
                    connection.settimeout(old_timeout)
                except OSError:
                    self.close_connection = True
        if read_error == "timeout":
            self.close_connection = True
            self._json(408, {"ok": False, "error": "请求正文读取超时"})
            return None
        if read_error in ("io", "short") or remaining:
            self.close_connection = True
            self._json(400, {"ok": False, "error": "请求正文不完整"})
            return None
        return b"".join(chunks)

    def _proxy_api(self):
        """审计修复④:/api/* 反向代理到 DSH web(带原 header,20s 超时),页面默认安装即可开箱即用"""
        stripped = self.path[6:] if self.path.startswith("/voice") else self.path  # P0 修复:剥前缀后转发
        upstream = API.rstrip("/") + stripped
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "accept-encoding",
                              "x-voice-token", "x-voice-pin"):
                continue
            headers[k] = v
        req = urllib.request.Request(upstream, data=None, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                status = resp.getcode()
                resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            status = e.code
            data = e.read()
            resp_headers = e.headers
        except Exception as e:
            return self._json(502, {"ok": False, "error": "代理失败: {}".format(str(e)[:100])})
        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if self._reject_query_credentials(parsed):
            return
        # /voice 前缀兼容(2026-08-24):tailscale serve 的 /voice 路径代理保留路径,这里剥掉前缀
        path_only = self._normalized_path(parsed.path)
        if path_only == "/reply":
            q = urllib.parse.parse_qs(parsed.query)
            if not self._check_auth():
                return self._json(403, {"ok": False, "error": "token 无效"})
            self._serve_reply()
            return
        if path_only.startswith("/api/"):
            q = urllib.parse.parse_qs(parsed.query)
            if not self._check_auth():
                return self._json(403, {"ok": False, "error": "token 无效"})
            self._proxy_api()
            return
        if path_only == "/send":
            q = urllib.parse.parse_qs(parsed.query)
            self._handle_send(q)
            return
        # 静态文件
        rel = urllib.parse.unquote(path_only).lstrip("/") or "index.html"
        path = os.path.realpath(os.path.join(ROOT, rel))
        if os.path.commonpath([path, ROOT]) != ROOT or not os.path.isfile(path):
            self._json(404, {"ok": False, "error": "not found"})
            return
        # 安全修复(七视角复审 8-23):静态服务只放行白名单扩展名,latest-reply.txt 等内部文件禁止直读
        allowed_ext = (".html", ".css", ".js", ".png", ".svg", ".ico", ".json", ".webmanifest")
        if not path.lower().endswith(allowed_ext):
            self._json(404, {"ok": False, "error": "not found"})
            return
        if path.endswith(".html"): ctype = "text/html; charset=utf-8"
        elif path.endswith(".shortcut"): ctype = "application/octet-stream"
        else: ctype = "text/plain; charset=utf-8"
        data = open(path, "rb").read()
        if path.endswith(".html"):
            # 安全修复(Codex 审计 8-23):真 token 只注入已鉴权请求,未鉴权访问拿到的是空占位
            authed = _token_matches(self.headers.get("X-Voice-Token"))
            data = data.replace(b"__VOICE_TOKEN__", TOKEN.encode("utf-8") if authed else b"")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_reply(self):
        try:
            data = open(os.path.join(ROOT, "latest-reply.txt"), "rb").read()
        except Exception:
            data = "(管家还没写回复)".encode("utf-8")
        import json as _json
        data = _json.dumps({"ok": True, "answer": data.decode("utf-8", "replace")}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if self._reject_query_credentials(parsed):
            return
        path = self._normalized_path(parsed.path)
        if path == "/auth" or path == "/send":
            body_limit = MAX_FORM_BODY
        elif path.startswith("/api/"):
            body_limit = MAX_API_BODY
        else:
            return self._json(404, {"ok": False, "error": "not found"})
        ln = self._validated_content_length(body_limit)
        if ln is None:
            return
        # /auth 必须先读受限 PIN body;其他 POST 必须先鉴权,不得读取未鉴权正文。
        if path != "/auth" and not self._check_auth():
            return self._json(403, {"ok": False, "error": "token 无效"})
        raw_body = self._read_body(ln)
        if raw_body is None:
            return
        body = raw_body.decode("utf-8", "replace")
        if path == "/auth":
            # 一次性配对码(2026-08-24):PWA 用 6 位码兑换 token;配对成功即作废码(文件删除+内存置空)
            if not TOKEN:
                return self._json(503, {"ok": False, "error": "服务 token 未配置"})
            q = urllib.parse.parse_qs(body)
            pin = (q.get("pin") or [""])[0]
            print(f"[voice-gate] /auth 请求: pin={'*'*len(pin) if pin else '空'}", file=sys.stderr, flush=True)
            source = self.client_address[0] if getattr(self, "client_address", None) else "<unknown>"
            outcome, retry = _consume_pairing_pin(source, pin)
            if outcome == "locked":
                return self._json(429, {"ok": False, "error": "配对尝试过多,来源已临时锁定",
                                        "retryAfter": retry}, {"Retry-After": str(retry)})
            if outcome == "ok":
                try:
                    os.remove(os.path.expanduser("~/.config/voice-gate.pin"))
                except FileNotFoundError:
                    pass
                except OSError:
                    _restore_pairing_pin(pin)
                    return self._json(500, {"ok": False, "error": "配对码持久作废失败,未发放 token"})
                return self._json(200, {"ok": True, "token": TOKEN, "paired": True})
            return self._json(403, {"ok": False, "error": "PIN 无效或已作废"})
        if path.startswith("/api/"):
            self._proxy_api_post(raw_body)
            return
        if path == "/send":
            q = urllib.parse.parse_qs(body)
            self._handle_send(q)
            return

    def _proxy_api_post(self, raw_body):
        """POST /api/* 代理:保留原始 body 转发"""
        stripped = self.path[6:] if self.path.startswith("/voice") else self.path  # P0 修复:剥前缀后转发
        upstream = API.rstrip("/") + stripped
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "accept-encoding",
                              "x-voice-token", "x-voice-pin"):
                continue
            headers[k] = v
        req = urllib.request.Request(upstream, data=raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                status = resp.getcode()
                resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            status = e.code
            data = e.read()
            resp_headers = e.headers
        except Exception as e:
            return self._json(502, {"ok": False, "error": "代理失败: {}".format(str(e)[:100])})
        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_send(self, q):
        if not self._check_auth():
            return self._json(403, {"ok": False, "error": "token 无效"})
        text = (q.get("text") or [""])[0].strip()
        if not text:
            return self._json(400, {"ok": False, "error": "text 为空"})
        if len(text) > MAX_LEN:
            return self._json(413, {"ok": False, "error": "太长"})
        try:
            answer, forward = route_intent(text)
            r = {"ok": True, "routed": "local" if forward is None else "inbox"}
            if answer:
                r["answer"] = answer
            if forward:
                requested_id = (q.get("sessionId") or [""])[0].strip() or None
                delivered = send_text(forward, requested_id=requested_id)
                if not delivered.get("ok"):
                    return self._json(502, delivered)
                r["routed"] = "voice-session"
                if delivered.get("sessionId"):
                    r["sessionId"] = delivered["sessionId"]
                if delivered.get("sessionSource"):
                    r["sessionSource"] = delivered["sessionSource"]
                if delivered.get("answer"):
                    r["answer"] = delivered["answer"]
                if delivered.get("pending"):
                    r["pending"] = True
                if delivered.get("requestId"):
                    r["requestId"] = delivered["requestId"]
            self._json(200, r)
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:120]})

    def _json(self, code, obj, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"ok":true,"svc":"voice-gate-health"}')
    def log_message(self, *a): pass

def _health_thread():
    try:
        ThreadingHTTPServer((BIND, HEALTH_PORT), HealthHandler).serve_forever()
    except Exception:
        pass

if __name__ == "__main__":
    os.makedirs(ROOT, exist_ok=True)
    _secure_reply_file()
    threading.Thread(target=_health_thread, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
