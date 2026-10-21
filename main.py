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

    POST /ai/chat      {messages, memory, snapshot, mode:'chat'|'reflect', voice}
                       -> text/event-stream: delta {text} · final {message} · error {message}
    GET  /ai/health    {ok, configured, model, gated}
    GET  /ai/spec      {system, reflect, tools, reflect_tools} — for the page's "your computer first" path

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
"""
import hashlib
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("PORT", 8080))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

AI_MODEL = os.environ.get("AIBO_AI_MODEL", "claude-opus-5-5")
AI_EFFORT = os.environ.get("AIBO_AI_EFFORT", "medium")
BACKEND = os.environ.get("AIBO_BACKEND", "https://api.bullwhistle.com").rstrip("/")
AI_OPEN = os.environ.get("AIBO_AI_OPEN", "") == "1"
MAX_BODY = 3 * 1024 * 1024
MAX_MESSAGES = 120

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
    req = urllib.request.Request(BACKEND + path, headers={"Authorization": "Bearer " + token, "User-Agent": "bullwhistle-ai/1"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def token_ok(token):
    """True when the backend accepts the token as an account session or the positions token."""
    if AI_OPEN:
        return True
    if not token or len(token) > 4096:
        return False
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with _gate_lock:
        hit = _gate.get(key)
        if hit and hit[1] > now:
            return hit[0]
    ok = None
    s = _probe("/auth/me", token)
    if s == 200:
        ok = True
    elif s in (401, 403, 404, 410):
        s2 = _probe("/positions.json", token)
        if s2 == 200:
            ok = True
        elif s2 in (401, 403, 404, 410):
            ok = False
    if ok is None:
        return None  # the backend could not be reached: say so, do not spend the key
    with _gate_lock:
        _gate[key] = (ok, now + GATE_TTL)
        if len(_gate) > 2000:
            for k in [k for k, v in _gate.items() if v[1] <= now]:
                _gate.pop(k, None)
    return ok


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

    def _bearer(self):
        a = self.headers.get("Authorization", "")
        return a[7:].strip() if a.lower().startswith("bearer ") else ""

    def _sse(self, event, obj):
        self.wfile.write(("event: " + event + "\ndata: " + json.dumps(obj) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    # ---- routes ----
    def do_GET(self):
        if self.path.split("?")[0] == "/ai/health":
            return self._json(200, {"ok": True, "configured": ai_configured(), "model": AI_MODEL, "gated": not AI_OPEN})
        if self.path.split("?")[0] == "/ai/spec":
            # the prompt and the tool definitions, for the page's "your computer first" path (a model on the person's own machine
            # runs the same instructions and the same tools; nothing here is secret)
            return self._json(200, {"system": SYSTEM_CHAT, "reflect": SYSTEM_REFLECT, "tools": TOOLS_CHAT, "reflect_tools": TOOLS_REFLECT})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/ai/chat":
            return self._json(404, {"error": "not found"})
        if not ai_configured():
            return self._json(503, {"error": "The assistant is not configured on this server (ANTHROPIC_API_KEY is not set)."})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._json(413 if n > MAX_BODY else 400, {"error": "bad body"})
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "bad json"})

        token = self._bearer()
        ok = token_ok(token)
        if ok is None:
            return self._json(503, {"error": "Could not verify your sign-in with the backend just now. Try again in a moment."})
        if not ok:
            return self._json(401, {"error": "The assistant needs your sign-in or the positions token on this device (Settings › Connected accounts)."})
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
                                            "stop_details": d.get("stop_details"), "model": d.get("model"), "usage": d.get("usage")}})
        except Exception as e:  # the page shows the sentence; the socket is already committed to SSE
            msg = ai_error_text(e)
            try:
                self._sse("error", {"message": msg})
            except Exception:
                pass
            print("ai/chat failed:", repr(e), file=sys.stderr)


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
        print("serving Hosting_Kit on :%d (assistant %s)" % (PORT, "on" if ai_configured() else "off: no ANTHROPIC_API_KEY"))
        httpd.serve_forever()
