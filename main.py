#!/usr/bin/env python3
"""Railway server for the hosted (SCRUBBED) AI Build-Out app.

Railway's nixpacks detects it and runs the kit at a public URL
(`railway up --service aibo-app` from this folder).
No-store headers so a fresh push goes live immediately; the PWA service worker
(sw.js) is NETWORK FIRST over the kit's own files only — its cache is the
offline / slow-network fallback, and it never caches the backend's answers.

10-20 — THE DESK ASSISTANT (the owner: "an interactive AI that has all the data of the
watchlist, the portfolio and the trades … learn from it over time … ask what assets are
doing certain things above certain indicators … give feedback after seeing wins and losses").

The assistant lives in the page (index.html, the BWAI module): the browser already holds
the person's tokens and every feed — positions, trades with FIFO P&L, lists, grades, the
per-symbol bar frames — so the browser RUNS the tools. This file is the only thing the
browser cannot be: the holder of the Anthropic key. It owns the system prompt and the tool
definitions (one place to tune), streams Claude's reply to the page as server-sent events,
and lets the page run each tool call and continue the turn.

    POST /ai/chat      {messages, memory, snapshot, mode:'chat'|'reflect', voice, mac}
                       -> text/event-stream: delta {text} · tool {name} · wait {where,state,elapsed} · final {message} · error {message}
    GET  /ai/health    {ok, configured, model, gated, mac:{configured, polling, seen_ago}}
    GET  /ai/spec      {system, reflect, tools, reflect_tools} — for the page's "your computer first" path
    GET  /ai/relay/pull?wait=20   the Mac relay claims the next job (Bearer = AIBO_MAC_RELAY_SECRET; 204 when none)
    POST /ai/relay/push?id=<job>  the Mac relay answers: {message} | {error} | {skip:true}

THE MAC RELAY (10-21 — the owner: "why can't my Claude session answer like the debrief's Mac poller does?"): a third
brain between the person's own computer and the cloud. When a chat arrives from someone the relay is switched on
for (the admin's account session or the owner's positions token, and the person's own `mac` flag), the server
parks the job in an in-memory queue and streams `wait` events while a poller on the owner's Mac (mac_relay/
aibo_mac_relay.py under launchd) claims it, runs `claude -p` on the Claude Code subscription — no API key spent —
and posts the model's turn back: either the answer text or the tool calls it wants (the browser runs them exactly as
it does for the cloud, same final-message shape, via:'mac', and continues the loop). Order of answering, as the page
sees it: Ollama on the same computer → the Mac relay → the cloud. A job nobody claims inside AIBO_MAC_RELAY_TTL
seconds (60) expires: the cloud answers when the key is set, otherwise the request fails with the honest sentence.
Nothing of the queue is readable without the relay secret (a wrong or missing one is a 404, same as no route).

"Your computer first": the page tries a model served by Ollama on the person's own machine before it
calls this endpoint at all (BWAI in index.html); the cloud answers only when the computer cannot.

Access is GATED on the app's own credentials: the request carries the person's positions
token or account session as a Bearer, and this server checks it against the backend once
per 15 minutes (GET /auth/me for a session, GET /positions.json for the owner's token).
No valid token, no model call — the key is never spent by a stranger who found the URL.

Environment (Railway → Variables):
    ANTHROPIC_API_KEY   required for the assistant (the static site works without it)
    AIBO_AI_MODEL       default claude-opus-5-5
    AIBO_AI_EFFORT      low | medium | high — default medium (voice turns run at low)
    AIBO_BACKEND        default https://api.bullwhistle.com (where the tokens are checked)
    AIBO_AI_OPEN        "1" skips the token gate (local development only)
    AIBO_MAC_RELAY_SECRET   switches the Mac relay on; the same string sits in ~/.aibo_relay_secret on the Mac
    AIBO_MAC_RELAY_TTL      seconds an unclaimed job lives (default 60); a claimed job may take up to 4× that to answer
"""
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = int(os.environ.get("PORT", 8080))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

AI_MODEL = os.environ.get("AIBO_AI_MODEL", "claude-opus-5-5")
AI_EFFORT = os.environ.get("AIBO_AI_EFFORT", "medium")
BACKEND = os.environ.get("AIBO_BACKEND", "https://api.bullwhistle.com").rstrip("/")
AI_OPEN = os.environ.get("AIBO_AI_OPEN", "") == "1"
MAX_BODY = 3 * 1024 * 1024
MAX_MESSAGES = 120
MAC_SECRET = os.environ.get("AIBO_MAC_RELAY_SECRET", "").strip()
RELAY_TTL = max(5.0, float(os.environ.get("AIBO_MAC_RELAY_TTL", "60") or 60))
RELAY_ANSWER_WAIT = RELAY_TTL * 4          # a claimed job: the CLI's own run time
RELAY_FRESH = 45.0                         # the poller pulled within this → "polling"

# ---------------------------------------------------------------------------
# The assistant's voice. Stable text first (cached), the person's memory after it.
# ---------------------------------------------------------------------------
SYSTEM_CHAT = """You are the desk assistant inside Bull Whistle, a private investment terminal. You sit next to one trader. You can see their portfolio, their executed trades with realized P&L, their watchlists, their journal, the app's grades on companies, and the price history and indicator reads for stocks and coins — but only through the tools. Use them. Never invent a price, a position, an indicator value, or a trade. If a tool says the data is missing or stale, say so plainly and work with what is there.

How you work
- Lead with the answer. Numbers go on their own line or in a short list, not buried in prose.
- When the person asks what is doing something (above an indicator, crossing a level, oversold, extended), run the screen tool with precise conditions over the universe they mean. "My stuff" means portfolio plus watchlists. If they do not name a universe, use portfolio and watchlists. Report the matches with the reads that qualified them, and say how many names were scanned and how many had no data.
- When the person asks about one name, call get_technicals and, if the app grades it, get_grades. Read the chart like a trader: trend across frames, where price sits against the moving averages, momentum, volatility, what would change your mind.
- You are also their coach. When they ask how they are doing, or for feedback, call get_trade_stats and get_trades, then speak candidly from their own numbers: what is working, what keeps costing them, the specific habit behind it (sizing, holding losers, chasing, over-trading one name, time of day), and one concrete thing to do next. Praise what deserves it. Do not flatter. Do not moralize.
- You remember this trader across conversations through the memory in your context. Use it: refer back to patterns, rules they set, what they said they would do. When you learn something durable about them (their style, a rule, a goal, a recurring mistake, a preference about how you talk to them) call remember. Do not call remember for ordinary facts the tools can recompute.
- Never give the person a reason to trust a number you did not just read. Say when a read is from the daily close versus live.
- Be direct and warm. Short sentences. No filler, no "great question", no disclaimers about not being a financial adviser — they know. Plain words over jargon; use the indicator names they used.
- In voice mode (the snapshot says so) answer in at most about 90 words of plain speech: no markdown, no tables, no symbols like %, spell units out ("percent", "dollars"), and offer to go deeper rather than reciting lists longer than five items.
- Indicator vocabulary the screen tool understands: close, chg_1 (last bar change percent), rsi14, ema21, ema55, sma20, sma50, sma200 (each also as dist_ema21_pct … distance of close from it in percent), macd, macd_signal, macd_hist, bb_pct_b (0 lower band … 1 upper band), bb_width_pct, atr14_pct, ret_5_pct, ret_20_pct, ret_60_pct, vol_ratio (last bar volume over its 20-bar average), stoch_k, adx14, dist_high_52_pct, dist_low_52_pct. Frames: d (daily), w (weekly), m (monthly), h4, h (hourly) — not every name has every frame. Crosses: crossed_above / crossed_below between two of those (close vs an average, macd vs macd_signal, rsi14 vs a level) within the last N bars.

The app, for context: Grade Flow / Atlas is the graded universe (expected-return and recommendation grades A+ … F with pillar grades G growth, P price/valuation, M moat, F financial strength, C cash, A allocation, B balance). Markets lists the US 500 stocks and the top 500 coins, ungraded. Portfolio Studio shows the book. The Macro desk holds the journal, alerts and trading history."""

SYSTEM_REFLECT = """You maintain the long-term memory of a trading desk assistant about one trader. You will be given the current memory, a transcript of a conversation, and sometimes fresh trade statistics. Rewrite the memory so the assistant knows this person better next time.

Call save_memory exactly once with:
- profile: the full, rewritten profile in plain prose and short bullet lines, at most 600 words. Keep everything still true from the old profile, fold in what the transcript showed, drop what the transcript contradicted. Cover: who they are as a trader (instruments, timeframes, style), what they are good at, what costs them money (be specific and quantified when the numbers support it), rules and goals they have stated, how they like to be spoken to, open threads to follow up on.
- insights: zero to four new one-line observations that are durable and not already in the profile (a habit, a tendency, a pattern in their wins or losses). Not restatements of tool output.
- summary: one or two sentences on what this conversation was about and anything the assistant promised or the trader decided.

Be candid and concrete; this memory is for the assistant, not for the trader to read, so no softening. Never store prices or indicator values — they go stale. Do not invent anything the transcript or the statistics do not support."""

TOOLS_CHAT = [
    {"name": "get_portfolio", "description": "The trader's current holdings from their connected brokers and exchanges, merged by symbol: quantity, dollar value, cost basis and unrealized P&L when the broker supplies it, the account it sits in, cash, totals, and how fresh the snapshot is. Call this before talking about what they own.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_trades", "description": "Executed trades, newest first, with the realized P&L the backend attached to each closing trade (FIFO) and the trader's own 'why' note when they wrote one. Filter by symbol or by recent days. Default 60 trades, maximum 300.",
     "input_schema": {"type": "object", "properties": {
         "symbol": {"type": "string", "description": "Only this ticker or coin."},
         "days": {"type": "integer", "description": "Only trades within the last N days."},
         "limit": {"type": "integer", "description": "How many to return (default 60, max 300)."}},
         "additionalProperties": False}},
    {"name": "get_trade_stats", "description": "Statistics computed over the trader's executed trades: count, win rate, average win and loss, expectancy, profit factor, largest win and loss, streaks, realized P&L by symbol (best and worst), by weekday and by hour of day, hold times from FIFO matching, position sizes, and the last 30 and 90 days. Use this for any feedback or 'how am I doing' question.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Restrict to the last N days (default: all)."},
         "symbol": {"type": "string", "description": "Restrict to one symbol."}},
         "additionalProperties": False}},
    {"name": "get_watchlists", "description": "The trader's lists (name and members) with each member's last price and one-day change where the app has them.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_grades", "description": "The app's grades and note for graded companies: expected-return grade, recommendation grade, the pillar grades (G P M F C A B), valuation read, confidence, sector layer, and the analyst note. Ungraded names come back marked ungraded.",
     "input_schema": {"type": "object", "properties": {
         "symbols": {"type": "array", "items": {"type": "string"}, "description": "Tickers or coin symbols, up to 40."}},
         "required": ["symbols"], "additionalProperties": False}},
    {"name": "get_technicals", "description": "Indicator reads for one symbol on each requested frame (d daily, w weekly, m monthly, h4, h), computed from the app's own bars: close and change, RSI 14, EMA 21/55 and SMA 20/50/200 with the distance of price from each, MACD, Bollinger %B and width, ATR percent, 5/20/60 bar returns, volume ratio, stochastic, ADX, distance from the 52-bar high and low, the last few bars, and the as-of time of the data.",
     "input_schema": {"type": "object", "properties": {
         "symbol": {"type": "string"},
         "frames": {"type": "array", "items": {"type": "string", "enum": ["d", "w", "m", "h4", "h"]}, "description": "Default ['d','w']."}},
         "required": ["symbol"], "additionalProperties": False}},
    {"name": "screen", "description": "Find the names that satisfy EVERY condition. Universe: 'portfolio', 'watchlists', 'mine' (both), 'graded' (every graded company), 'stocks' (the US 500), 'coins' (the top 500 coins), 'list' (one named list, give list_name), or 'symbols' (give symbols). Each condition compares a metric on a frame with a number, or with another metric, or asks for a cross within the last N bars. Returns the matches with the reads that qualified them, the count scanned, and the names that had no data on that frame. Large universes take a few seconds; the daily frame is the one most names carry.",
     "input_schema": {"type": "object", "properties": {
         "universe": {"type": "string", "enum": ["portfolio", "watchlists", "mine", "graded", "stocks", "coins", "list", "symbols"]},
         "list_name": {"type": "string"},
         "symbols": {"type": "array", "items": {"type": "string"}},
         "conditions": {"type": "array", "items": {"type": "object", "properties": {
             "metric": {"type": "string", "description": "e.g. rsi14, close, dist_ema55_pct, macd_hist, bb_pct_b, atr14_pct, ret_20_pct, vol_ratio, adx14, stoch_k"},
             "frame": {"type": "string", "enum": ["d", "w", "m", "h4", "h"], "description": "Default d."},
             "op": {"type": "string", "enum": ["<", "<=", ">", ">=", "between", "crossed_above", "crossed_below"]},
             "value": {"type": "number", "description": "The number to compare with (for between: the low end)."},
             "value2": {"type": "number", "description": "between: the high end."},
             "ref": {"type": "string", "description": "Instead of a number, another metric on the same frame (e.g. close > ema55, macd crossed_above macd_signal)."},
             "within": {"type": "integer", "description": "For crosses: within the last N bars (default 3)."}},
             "required": ["metric", "op"], "additionalProperties": False}},
         "sort_by": {"type": "string", "description": "A metric to sort matches by (default: the first condition's metric)."},
         "descending": {"type": "boolean"},
         "limit": {"type": "integer", "description": "Max matches to return (default 25, max 60)."}},
         "required": ["universe", "conditions"], "additionalProperties": False}},
    {"name": "get_journal", "description": "The trader's free-text journal entries (newest first) and their price alerts.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Default 20."}}, "additionalProperties": False}},
    {"name": "get_market_overview", "description": "The wider tape: breadth and the biggest one-day movers among the US 500 stocks and the top coins, plus the backend's status block (dates, counts, the earnings watch). Use it for 'what is moving today' and for context before judging a single name.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "remember", "description": "Store something durable about this trader in long-term memory: a rule they set, a goal, a recurring pattern you noticed, a preference about how they want to be spoken to, a plan to follow up on. One short sentence. Not for prices or indicator values.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["rule", "goal", "pattern", "preference", "followup", "profile"]},
         "text": {"type": "string"}},
         "required": ["kind", "text"], "additionalProperties": False}},
    {"name": "open_in_app", "description": "Open a symbol's page in the app (the company page for a graded company, the card for anything else) so the trader can look at it. Use when they ask to see or show a name.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"], "additionalProperties": False}},
]

TOOLS_REFLECT = [
    {"name": "save_memory", "description": "Save the rewritten long-term memory.",
     "input_schema": {"type": "object", "properties": {
         "profile": {"type": "string"},
         "insights": {"type": "array", "items": {"type": "string"}},
         "summary": {"type": "string"}},
         "required": ["profile", "insights", "summary"], "additionalProperties": False}},
]

# ---------------------------------------------------------------------------
# Anthropic client (lazy, so the static site serves even without the SDK or a key)
# ---------------------------------------------------------------------------
_client = None
_client_lock = threading.Lock()


def anthropic_client():
    global _client
    with _client_lock:
        if _client is None:
            import anthropic  # noqa: WPS433 (lazy on purpose)
            _client = anthropic.Anthropic()
        return _client


def ai_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# ---------------------------------------------------------------------------
# The token gate: the app's own credentials, checked against the backend, cached 15 min
# ---------------------------------------------------------------------------
_gate = {}
_gate_lock = threading.Lock()
GATE_TTL = 15 * 60


def _probe(path, token):
    """(status, body-dict-or-None): the backend's answer for this token."""
    req = urllib.request.Request(BACKEND + path, headers={"Authorization": "Bearer " + token, "User-Agent": "bullwhistle-ai/1"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read(65536)
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = None
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def gate(token):
    """(ok, admin): ok = the backend accepts the token as an account session or the positions token; admin = the
    owner (an admin account per /auth/me, or the positions token itself). None when the backend could not be reached."""
    if AI_OPEN:
        return True, True
    if not token or len(token) > 4096:
        return False, False
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with _gate_lock:
        hit = _gate.get(key)
        if hit and hit[2] > now:
            return hit[0], hit[1]
    ok, admin = None, False
    s, me = _probe("/auth/me", token)
    if s == 200:
        ok = True
        admin = bool(isinstance(me, dict) and me.get("admin"))
    elif s in (401, 403, 404, 410):
        s2, _ = _probe("/positions.json", token)
        if s2 == 200:
            ok, admin = True, True
        elif s2 in (401, 403, 404, 410):
            ok = False
    if ok is None:
        return None, False  # the backend could not be reached: say so, do not spend the key
    with _gate_lock:
        _gate[key] = (ok, admin, now + GATE_TTL)
        if len(_gate) > 2000:
            for k in [k for k, v in _gate.items() if v[2] <= now]:
                _gate.pop(k, None)
    return ok, admin


def token_ok(token):
    return gate(token)[0]


# a soft rate limit per token: 40 model calls per 10 minutes
_rate = {}
_rate_lock = threading.Lock()


def rate_ok(token):
    key = hashlib.sha256((token or "anon").encode("utf-8")).hexdigest()
    now = time.time()
    with _rate_lock:
        q = [t for t in _rate.get(key, []) if t > now - 600]
        if len(q) >= 40:
            _rate[key] = q
            return False
        q.append(now)
        _rate[key] = q
        return True


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------
def clean_messages(msgs):
    """Only user/assistant turns, as the page sent them (content blocks pass through so thinking
    and tool_use blocks ride back unchanged — the history stays append-only)."""
    if not isinstance(msgs, list) or not msgs:
        raise ValueError("messages missing")
    out = []
    for m in msgs[-MAX_MESSAGES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise ValueError("bad message role")
        c = m.get("content")
        if not (isinstance(c, str) or isinstance(c, list)):
            raise ValueError("bad message content")
        out.append({"role": m["role"], "content": c})
    if out[0]["role"] != "user":
        raise ValueError("first message must be the user's")
    return out


def build_system(mode, memory):
    base = SYSTEM_CHAT if mode == "chat" else SYSTEM_REFLECT
    blocks = [{"type": "text", "text": base, "cache_control": {"type": "ephemeral"}}]
    memory = (memory or "").strip()
    if memory and mode == "chat":
        blocks.append({"type": "text", "text": "What you remember about this trader (keep using it; update it with the remember tool when you learn something durable):\n\n" + memory[:24000], "cache_control": {"type": "ephemeral"}})
    return blocks


def message_to_dict(msg):
    try:
        return msg.to_dict()
    except Exception:
        return json.loads(msg.model_dump_json())


def system_text(mode, memory):
    """The same system prompt as a single string, for a model that takes no content blocks (the Mac relay)."""
    return "\n\n".join(b["text"] for b in build_system(mode, memory))


# ---------------------------------------------------------------------------
# The Mac relay queue (10-21). In memory, one process: Railway runs one instance of this kit.
# ---------------------------------------------------------------------------
class Relay(object):
    def __init__(self):
        self.jobs = {}
        self.cv = threading.Condition()
        self.seen = 0.0            # the poller's last pull
        self.seen_at_least_once = False

    def configured(self):
        return bool(MAC_SECRET)

    def authorized(self, bearer):
        return bool(MAC_SECRET) and bool(bearer) and hmac.compare_digest(bearer, MAC_SECRET)

    def polling(self):
        return self.configured() and (time.time() - self.seen) < RELAY_FRESH

    def seen_ago(self):
        return round(time.time() - self.seen, 1) if self.seen else None

    def _reap(self, now):
        for jid in [j for j, job in self.jobs.items() if job["state"] in ("done", "gone") and now - job["created"] > RELAY_ANSWER_WAIT + 60]:
            self.jobs.pop(jid, None)

    def enqueue(self, payload):
        jid = secrets.token_hex(16)
        with self.cv:
            self.jobs[jid] = {"id": jid, "created": time.time(), "state": "queued", "payload": payload, "result": None, "claimed": None}
            self._reap(time.time())
            self.cv.notify_all()
        return jid

    def pull(self, wait, alive=None):
        """The poller's long poll: the oldest queued, unexpired job, or None after `wait` seconds. `alive()` says whether
        the poller is still on the line — a poller killed mid-poll leaves this thread behind for up to `wait` seconds,
        and it must not claim a job nobody will answer (the harness caught exactly that)."""
        deadline = time.time() + max(0.0, min(float(wait or 0), 25.0))
        with self.cv:
            self.seen = time.time()
            self.seen_at_least_once = True
            while True:
                now = time.time()
                if alive and not alive():
                    return None
                for job in sorted(self.jobs.values(), key=lambda j: j["created"]):
                    if job["state"] == "queued":
                        if now - job["created"] > RELAY_TTL:
                            job["state"] = "gone"
                            continue
                        job["state"] = "claimed"
                        job["claimed"] = now
                        self.seen = now
                        self.cv.notify_all()
                        return dict(job["payload"], id=job["id"], created=job["created"])
                left = deadline - now
                if left <= 0:
                    self.seen = time.time()
                    return None
                self.cv.wait(min(left, 5.0))

    def push(self, jid, result):
        with self.cv:
            job = self.jobs.get(jid)
            if not job or job["state"] not in ("queued", "claimed"):
                return False
            job["result"] = result
            job["state"] = "done"
            self.seen = time.time()
            self.cv.notify_all()
            return True

    def withdraw(self, jid):
        with self.cv:
            job = self.jobs.get(jid)
            if job and job["state"] in ("queued", "claimed"):
                job["state"] = "gone"
            self.cv.notify_all()

    def wait_result(self, jid, tick):
        """Block until the job is answered or expired. `tick(state, elapsed)` runs about every 2 s so the chat
        handler can stream a `wait` event (and notice a dropped browser). Returns (state, result)."""
        with self.cv:
            while True:
                job = self.jobs.get(jid)
                if not job:
                    return "gone", None
                now = time.time()
                elapsed = now - job["created"]
                if job["state"] == "done":
                    return "done", job["result"]
                if job["state"] == "gone":
                    return "gone", None
                if job["state"] == "queued" and elapsed > RELAY_TTL:
                    job["state"] = "gone"
                    return "unclaimed", None
                if job["state"] == "claimed" and now - job["claimed"] > RELAY_ANSWER_WAIT:
                    job["state"] = "gone"
                    return "timeout", None
                self.cv.release()
                try:
                    tick(job["state"], elapsed)
                finally:
                    self.cv.acquire()
                self.cv.wait(2.0)


relay = Relay()


def relay_message_ok(m):
    """The poller's answer in the cloud's final-message shape: text and tool_use blocks only, a known stop reason."""
    if not isinstance(m, dict) or not isinstance(m.get("content"), list) or not m["content"]:
        return False
    if m.get("stop_reason") not in ("end_turn", "tool_use", "max_tokens"):
        return False
    for b in m["content"]:
        if not isinstance(b, dict):
            return False
        if b.get("type") == "text":
            if not isinstance(b.get("text"), str):
                return False
        elif b.get("type") == "tool_use":
            if not (isinstance(b.get("id"), str) and isinstance(b.get("name"), str) and isinstance(b.get("input"), dict)):
                return False
        else:
            return False
    return True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # 10-07 sweep: no other site may frame the app — a framing page could overlay its own UI on the
        # bearer-backed one-tap controls (Delete list, Sign out everywhere). Same-origin frames (yields.html) still load.
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

    # ---- json helpers ----
    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _peer_alive(self):
        """False once the client has closed its side (a FIN shows as a zero-byte read); True while it is only quiet."""
        try:
            self.connection.setblocking(False)
            try:
                data = self.connection.recv(1, socket.MSG_PEEK)
            finally:
                self.connection.setblocking(True)
            return data != b""
        except (BlockingIOError, InterruptedError):
            return True
        except Exception:
            return False

    def _bearer(self):
        a = self.headers.get("Authorization", "")
        return a[7:].strip() if a.lower().startswith("bearer ") else ""

    def _sse(self, event, obj):
        self.wfile.write(("event: " + event + "\ndata: " + json.dumps(obj) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    # ---- routes ----
    def _query(self):
        q = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    q[k] = urllib.parse.unquote(v)
        return q

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ai/health":
            return self._json(200, {"ok": True, "configured": ai_configured(), "model": AI_MODEL, "gated": not AI_OPEN,
                                    "mac": {"configured": relay.configured(), "polling": relay.polling(), "seen_ago": relay.seen_ago()}})
        if path == "/ai/relay/pull":
            # the Mac's long poll. No secret on the server, or the wrong one: 404, as if the route did not exist.
            if not relay.authorized(self._bearer()):
                return self._json(404, {"error": "not found"})
            try:
                wait = float(self._query().get("wait", "20"))
            except ValueError:
                wait = 20.0
            job = relay.pull(wait, self._peer_alive)
            if job is None:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json(200, {"job": job})
        if self.path.split("?")[0] == "/ai/spec":
            # the prompt and the tool definitions, for the page's "your computer first" path (a model on the person's own machine
            # runs the same instructions and the same tools; nothing here is secret)
            return self._json(200, {"system": SYSTEM_CHAT, "reflect": SYSTEM_REFLECT, "tools": TOOLS_CHAT, "reflect_tools": TOOLS_REFLECT})
        return super().do_GET()

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._json(413 if n > MAX_BODY else 400, {"error": "bad body"})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._json(400, {"error": "bad json"})
            return None

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/ai/relay/push":
            return self.relay_push()
        if path != "/ai/chat":
            return self._json(404, {"error": "not found"})
        req = self._read_json()
        if req is None:
            return

        token = self._bearer()
        ok, admin = gate(token)
        if ok is None:
            return self._json(503, {"error": "Could not verify your sign-in with the backend just now. Try again in a moment."})
        if not ok:
            return self._json(401, {"error": "The assistant needs your sign-in or the positions token on this device (Settings › Connected accounts)."})

        # who answers: the Mac relay for the owner when it is switched on and polling, else the cloud, else the honest 503
        mac_wanted = relay.configured() and admin and req.get("mac", True) is not False
        route = "mac" if (mac_wanted and relay.polling()) else ("cloud" if ai_configured() else None)
        if route is None:
            if mac_wanted:
                ago = relay.seen_ago()
                return self._json(503, {"error": "Your Mac is not answering right now (the relay last polled " + (str(int(ago)) + " s ago" if ago is not None else "never") + ") and this server has no Anthropic key."})
            return self._json(503, {"error": "The assistant is not configured on this server (ANTHROPIC_API_KEY is not set)."})
        if not rate_ok(token):
            return self._json(429, {"error": "Slow down a little — the assistant takes a short breather after 40 questions in ten minutes."})

        mode = "reflect" if req.get("mode") == "reflect" else "chat"
        try:
            messages = clean_messages(req.get("messages"))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        voice = bool(req.get("voice"))
        effort = "low" if (voice and mode == "chat") else AI_EFFORT
        tools = TOOLS_CHAT if mode == "chat" else TOOLS_REFLECT

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        if route == "mac":
            state = self.chat_via_mac(req, mode, messages, voice, effort, tools)
            if state == "done":
                return
            # unclaimed / timed out / the Mac declined: the cloud when there is a key, else say so
            if not ai_configured():
                try:
                    self._sse("error", {"message": {"unclaimed": "Your Mac did not pick the question up in time, and this server has no Anthropic key.",
                                                    "timeout": "Your Mac took too long to answer, and this server has no Anthropic key.",
                                                    "skip": "Your Mac declined this one, and this server has no Anthropic key."}.get(state, "Your Mac did not answer.")})
                except Exception:
                    pass
                return
            try:
                self._sse("wait", {"where": "cloud", "state": state, "elapsed": 0})
            except Exception:
                return
        try:
            client = anthropic_client()
            with client.beta.messages.stream(
                model=AI_MODEL,
                max_tokens=16000,
                system=build_system(mode, req.get("memory")),
                messages=messages,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        self._sse("delta", {"text": event.delta.text})
                    elif event.type == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
                        self._sse("tool", {"name": event.content_block.name})
                final = stream.get_final_message()
            d = message_to_dict(final)
            self._sse("final", {"message": {"role": "assistant", "content": d.get("content", []), "stop_reason": d.get("stop_reason"),
                                            "stop_details": d.get("stop_details"), "model": d.get("model"), "usage": d.get("usage"), "via": "cloud"}})
        except Exception as e:  # the page shows the sentence; the socket is already committed to SSE
            msg = ai_error_text(e)
            try:
                self._sse("error", {"message": msg})
            except Exception:
                pass
            print("ai/chat failed:", repr(e), file=sys.stderr)

    # ---- the Mac relay ----
    def chat_via_mac(self, req, mode, messages, voice, effort, tools):
        """Park the turn for the Mac, stream `wait` while it works, relay its answer as `final`. Returns the job's end
        state: done | unclaimed | timeout | skip | error (the caller falls back to the cloud for everything but done)."""
        memory = (req.get("memory") or "") if mode == "chat" else ""
        jid = relay.enqueue({"mode": mode, "voice": voice, "effort": effort, "system": system_text(mode, memory),
                             "messages": messages, "tools": tools})
        dropped = []

        def tick(state, elapsed):
            try:
                self._sse("wait", {"where": "mac", "state": state, "elapsed": int(elapsed)})
            except Exception:
                dropped.append(1)
                relay.withdraw(jid)

        state, result = relay.wait_result(jid, tick)
        if dropped:
            return "done"
        if state != "done":
            return state
        if not isinstance(result, dict):
            return "error"
        if result.get("skip"):
            return "skip"
        if result.get("error"):
            try:
                self._sse("error", {"message": "Your Mac answered with an error: " + str(result["error"])[:300]})
            except Exception:
                pass
            return "done"
        m = result.get("message")
        if not relay_message_ok(m):
            try:
                self._sse("error", {"message": "Your Mac sent an answer this server could not read."})
            except Exception:
                pass
            return "done"
        try:
            self._sse("final", {"message": {"role": "assistant", "content": m["content"], "stop_reason": m["stop_reason"], "stop_details": None,
                                            "model": str(result.get("model") or "claude-code")[:80], "usage": result.get("usage") if isinstance(result.get("usage"), dict) else None,
                                            "via": "mac"}})
        except Exception:
            pass
        return "done"

    def relay_push(self):
        if not relay.authorized(self._bearer()):
            return self._json(404, {"error": "not found"})
        jid = self._query().get("id", "")
        if not (len(jid) == 32 and all(c in "0123456789abcdef" for c in jid)):
            return self._json(400, {"error": "bad id"})
        body = self._read_json()
        if body is None:
            return
        if not isinstance(body, dict) or not (body.get("skip") or body.get("error") or "message" in body):
            return self._json(400, {"error": "expected {message} | {error} | {skip:true}"})
        if "message" in body and not body.get("skip") and not body.get("error") and not relay_message_ok(body.get("message")):
            return self._json(400, {"error": "bad message shape: content = text | tool_use blocks, stop_reason = end_turn | tool_use | max_tokens"})
        if not relay.push(jid, body):
            return self._json(410, {"error": "that job is gone (expired, withdrawn or already answered)"})
        return self._json(200, {"ok": True})


def ai_error_text(e):
    try:
        import anthropic
        if isinstance(e, anthropic.AuthenticationError):
            return "The server's Anthropic key was not accepted."
        if isinstance(e, anthropic.RateLimitError):
            return "The model is rate-limited right now. Try again in a minute."
        if isinstance(e, anthropic.APIStatusError):
            return "The model returned an error (" + str(e.status_code) + ")."
        if isinstance(e, anthropic.APIConnectionError):
            return "The model could not be reached."
    except Exception:
        pass
    return "The assistant hit an error: " + str(e)[:200]


socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer.daemon_threads = True

if __name__ == "__main__":
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        print("serving Hosting_Kit on :%d (assistant %s; Mac relay %s)" % (PORT, "on" if ai_configured() else "off: no ANTHROPIC_API_KEY", "on" if relay.configured() else "off: no AIBO_MAC_RELAY_SECRET"))
        httpd.serve_forever()
