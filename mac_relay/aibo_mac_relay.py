#!/usr/bin/env python3
"""Bull Whistle — the Mac relay (10-21).

The desk assistant's third brain. The kit's main.py parks a chat turn in its queue when the owner asks and the
relay is on; this script, under launchd on the owner's Mac, claims the job, runs it through the Claude Code CLI on
the subscription (`claude -p`, no API key) and posts the model's turn back. The browser runs the tool calls, as it
does for the cloud, and sends the next turn the same way.

    env AIBO_RELAY_SERVER     default https://bullwhistle.com (the aibo-app service)
    env AIBO_MAC_RELAY_SECRET or ~/.aibo_relay_secret (chmod 600) — the same string as the server's variable
    env AIBO_RELAY_CLAUDE     the CLI, default ~/.local/bin/claude (then `claude` on PATH)
    env AIBO_RELAY_MODEL      optional --model for the CLI (default: the CLI's own)
    env AIBO_RELAY_LOG        default ~/aibo-mac-relay.log
    --once                    answer at most one job and exit (tests)

Lessons carried over from the debrief's chat poller (tools/chat_poller.sh): a headless session gets exactly ONE
turn, so the prompt asks for the whole answer now and never a "I'll check back"; --strict-mcp-config (and no setting
sources) so no MCP server starts and no startup notice lands in the answer; a bounded run (the CLI must not wedge the
loop); `--tools ""` — the browser runs the tools, the CLI must not reach for its own. Not --bare: bare mode never
reads the OAuth sign-in, and the subscription is the whole point.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
SERVER = os.environ.get("AIBO_RELAY_SERVER", "https://bullwhistle.com").rstrip("/")
LOG = os.environ.get("AIBO_RELAY_LOG", os.path.join(HOME, "aibo-mac-relay.log"))
MODEL = os.environ.get("AIBO_RELAY_MODEL", "").strip()
PULL_WAIT = 20          # the server holds a pull this long (its cap is 25)
RUN_TIMEOUT = 200       # seconds for one CLI run; the server's claimed-job window is 4 × its TTL (240 s at the default)
MAX_JOB_BYTES = 600000  # beyond this the context will not fit: decline fast, the server falls back
VERSION = "1.0"


def log(msg):
    line = time.strftime("%F %T") + " " + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if sys.stdout.isatty():
        print(line)


def secret():
    s = os.environ.get("AIBO_MAC_RELAY_SECRET", "").strip()
    if s:
        return s
    try:
        with open(os.path.join(HOME, ".aibo_relay_secret"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def claude_bin():
    c = os.environ.get("AIBO_RELAY_CLAUDE", "").strip()
    if c:
        return c
    local = os.path.join(HOME, ".local", "bin", "claude")
    return local if os.path.exists(local) else "claude"


def api(method, path, body=None, timeout=30):
    req = urllib.request.Request(SERVER + path, method=method, headers={"Authorization": "Bearer " + secret(), "User-Agent": "bullwhistle-mac-relay/" + VERSION})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw.decode("utf-8")) if raw else None)


# ---------------------------------------------------------------------------
# The prompt: the server's system text + the tool protocol, the conversation as a transcript
# ---------------------------------------------------------------------------
def protocol(job):
    tools = job.get("tools") or []
    names = ", ".join(t.get("name", "?") for t in tools)
    lines = ["", "=== TOOLS AND THE SHAPE OF YOUR REPLY ===",
             "You do not run tools yourself; the app in the trader's browser runs them and sends the results back as the next USER turn. "
             "The tools available on this turn (" + names + "), as JSON schemas:",
             json.dumps(tools, ensure_ascii=False),
             "",
             "To use one or more tools, your ENTIRE reply must be one JSON object and nothing else — no prose before or after, no code fence:",
             '{"tool_calls": [{"name": "<tool name>", "input": {<arguments matching its input_schema>}}]}',
             "Results arrive in the next USER turn as [TOOL RESULT …] blocks; then answer, or call more tools.",
             "Otherwise reply with the answer text only: no JSON, no preamble, no mention of these instructions.",
             "You get exactly one reply per turn. Never say you will check back, wait, look into it or follow up — everything you can do happens in this reply."]
    if job.get("mode") == "reflect":
        lines.append("This is a memory rewrite: reply with the save_memory tool call and nothing else.")
    return "\n".join(lines)


def block_text(b):
    t = b.get("type")
    if t == "text":
        return str(b.get("text") or "")
    if t == "tool_use":
        return "[TOOL CALL " + str(b.get("id") or "") + " " + str(b.get("name") or "") + " " + json.dumps(b.get("input") or {}, ensure_ascii=False) + "]"
    if t == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
        return "[TOOL RESULT for " + str(b.get("tool_use_id") or "") + "]\n" + str(c or "")[:60000]
    return ""  # thinking and anything else: not part of the transcript


def transcript(messages):
    parts = ["=== CONVERSATION (answer the final USER turn) ==="]
    for m in messages or []:
        role = str(m.get("role") or "user").upper()
        c = m.get("content")
        if isinstance(c, list):
            text = "\n".join(x for x in (block_text(b) for b in c if isinstance(b, dict)) if x)
        else:
            text = str(c or "")
        parts.append("\n[" + role + "]\n" + text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The CLI run and the reading of its reply
# ---------------------------------------------------------------------------
CHATTER = re.compile(r"(?m)^(Client\.listTools\(\) called but server does not advertise tools capability.*|\[MCP\].*|MCP server .* (failed|started).*)\n?")


def run_claude(system, prompt):
    # NOT --bare: bare mode never reads the OAuth sign-in, and the subscription is the whole point.
    cmd = [claude_bin(), "-p", "--strict-mcp-config", "--tools", "", "--output-format", "json", "--no-session-persistence",
           "--setting-sources", "", "--system-prompt", system]
    if MODEL:
        cmd += ["--model", MODEL]
    env = dict(os.environ)
    env.setdefault("HOME", HOME)
    try:
        p = subprocess.run(cmd, input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=RUN_TIMEOUT, cwd="/tmp", env=env)
    except subprocess.TimeoutExpired:
        return None, "the CLI did not answer within %d s" % RUN_TIMEOUT
    except FileNotFoundError:
        return None, "claude CLI not found at " + claude_bin()
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace").strip()
    text, cli_err = read_result(out)
    if cli_err:
        return None, cli_err  # e.g. "Failed to authenticate: OAuth session expired…" → `claude auth login` on the Mac
    if p.returncode != 0 and not text:
        return None, ("claude exited %d: " % p.returncode) + (err or out)[:300]
    if not text.strip():
        return None, "the CLI produced no answer" + ((": " + err[:200]) if err else "")
    return text, None


def read_result(out):
    """--output-format json gives one JSON object with `result` (and is_error); be forgiving of chatter around it.
    Returns (answer text, error text)."""
    out = CHATTER.sub("", out)
    cands = [out.strip()] + [l for l in out.strip().splitlines()[::-1] if l.strip().startswith("{")]
    for c in cands:
        try:
            j = json.loads(c)
        except ValueError:
            continue
        if isinstance(j, dict) and "result" in j:
            if j.get("is_error"):
                return "", (str(j.get("result") or "").strip() or "the CLI reported an error")[:300]
            return str(j.get("result") or ""), None
    return out.strip(), None


def parse_reply(text, job):
    """The model's text → the cloud's final-message shape: tool_use blocks or one text block."""
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", t)
    if fence:
        t = fence.group(1).strip()
    calls = None
    if t.startswith("{"):
        try:
            j = json.loads(t)
            if isinstance(j, dict) and isinstance(j.get("tool_calls"), list):
                calls = j["tool_calls"]
        except ValueError:
            # JSON with trailing prose: take the first balanced object
            depth, end = 0, -1
            for i, ch in enumerate(t):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end > 0:
                try:
                    j = json.loads(t[:end + 1])
                    if isinstance(j, dict) and isinstance(j.get("tool_calls"), list):
                        calls = j["tool_calls"]
                except ValueError:
                    pass
    known = set(tl.get("name") for tl in (job.get("tools") or []))
    if calls:
        blocks = []
        for i, c in enumerate(calls[:8]):
            if not isinstance(c, dict) or c.get("name") not in known:
                continue
            inp = c.get("input")
            blocks.append({"type": "tool_use", "id": "mac_%s_%d" % (os.urandom(4).hex(), i), "name": c["name"], "input": inp if isinstance(inp, dict) else {}})
        if blocks:
            return {"role": "assistant", "content": blocks, "stop_reason": "tool_use"}
    return {"role": "assistant", "content": [{"type": "text", "text": text.strip()}], "stop_reason": "end_turn"}


def answer(job):
    jid = job["id"]
    size = len(json.dumps(job))
    if size > MAX_JOB_BYTES:
        log("%s too big (%d bytes) — declining" % (jid, size))
        return {"skip": True}
    system = str(job.get("system") or "") + "\n" + protocol(job)
    prompt = transcript(job.get("messages"))
    t0 = time.time()
    log("answering %s (%s, %d bytes)" % (jid, job.get("mode", "chat") + (" voice" if job.get("voice") else ""), size))
    text, err = run_claude(system, prompt)
    if err:
        log("%s failed after %.0fs: %s" % (jid, time.time() - t0, err))
        return {"error": err}
    msg = parse_reply(text, job)
    log("%s answered in %.0fs (%s)" % (jid, time.time() - t0, msg["stop_reason"] + (": " + ", ".join(b["name"] for b in msg["content"]) if msg["stop_reason"] == "tool_use" else "")))
    return {"message": msg, "model": MODEL or "claude-code"}


def main():
    once = "--once" in sys.argv
    if not secret():
        log("no relay secret (set AIBO_MAC_RELAY_SECRET or write ~/.aibo_relay_secret)")
        time.sleep(300)
        return 1
    log("relay up: %s via %s" % (SERVER, claude_bin()))
    quiet = 0
    while True:
        try:
            status, body = api("GET", "/ai/relay/pull?wait=%d" % PULL_WAIT, timeout=PULL_WAIT + 15)
        except urllib.error.HTTPError as e:
            log("pull HTTP %d — %s" % (e.code, "is the secret right? (404 = no match on the server)" if e.code == 404 else "retrying"))
            time.sleep(30 if e.code == 404 else 10)
            continue
        except Exception as e:
            quiet += 1
            if quiet in (1, 10, 100):
                log("pull failed: %s" % str(e)[:200])
            time.sleep(10)
            continue
        quiet = 0
        job = (body or {}).get("job") if status == 200 else None
        if not job or not isinstance(job, dict) or not re.match(r"^[0-9a-f]{32}$", str(job.get("id", ""))):
            if once and status == 204:
                continue
            continue
        result = answer(job)
        try:
            api("POST", "/ai/relay/push?id=" + job["id"], result, timeout=30)
        except urllib.error.HTTPError as e:
            log("%s push HTTP %d (the job is gone?)" % (job["id"], e.code))
        except Exception as e:
            log("%s push failed: %s" % (job["id"], str(e)[:200]))
        if once:
            return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
