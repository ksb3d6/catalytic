# The Mac relay — answer the desk assistant from your own Claude Code subscription

The relay is a small poller on your Mac. The app's server (main.py on the aibo-app service) parks each of your
questions in a queue; the poller claims it, runs it through `claude -p` on your Claude Code subscription, and posts
the answer back. No Anthropic API key is spent. The browser still runs every tool (your book, trades, screens) and
the answer shows "your Mac" under it. Only the admin's questions go this way; anyone else gets the cloud as before.

Order of answering, as the app sees it: a model on this computer (Ollama) → the Mac relay → the cloud (the API key).
When the Mac is asleep or the relay is not loaded, the cloud answers; with no key either, the sheet says so.

## Once

1. **Sign the CLI in** on the Mac (the desktop app's sign-in is not shared with the CLI):
   ```bash
   ~/.local/bin/claude auth login
   ```
   `~/.local/bin/claude auth status` must say `loggedIn: true`.
2. **Make a secret** and put it in both places:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))" | tee ~/.aibo_relay_secret; chmod 600 ~/.aibo_relay_secret
   ```
   Railway → aibo-app → Variables → `AIBO_MAC_RELAY_SECRET` = that string (then the service restarts).
3. **Load the poller** (copies the script to `~/.local/bin`, writes the plist with your home folder, loads it):
   ```bash
   ./install.sh
   ```
   `GET https://bullwhistle.com/ai/health` → `mac.polling: true` within ~20 s. The log is `~/aibo-mac-relay.log`.
4. In the app: Ask → Memory → "Your Mac next" → Test → "ready: Mac relay (Claude Code)". The sheet's status dot
   turns violet and reads "Mac".

## Day to day
- The poller runs while the Mac is awake and you are logged in (launchd KeepAlive, 20-second long polls, ~nothing idle).
- Stop: `launchctl unload ~/Library/LaunchAgents/com.bullwhistle.mac-relay.plist`. Start: `load`.
- A question the relay does not pick up within 60 s (Mac asleep, poller stopped) falls back to the cloud; the app's
  wait line counts the seconds. One CLI run is bounded at 200 s.
- Switch it off for yourself without touching the Mac: Ask → Memory → untick "Answer through my Mac".
- Optional: `AIBO_RELAY_MODEL` in the plist's EnvironmentVariables picks the CLI model; `AIBO_RELAY_SERVER` points a
  second copy at a local kit (`http://127.0.0.1:8499`).

## How a turn works (for whoever edits this)
`GET /ai/relay/pull?wait=20` (Bearer = the secret) → `{job: {id, mode, voice, system, messages, tools}}` or 204 →
the poller writes the system text + a tool protocol and the conversation as a transcript → `claude -p
--strict-mcp-config --setting-sources "" --tools "" --output-format json --no-session-persistence --system-prompt …`
(never `--bare`: it refuses the OAuth sign-in) → the reply is either
`{"tool_calls":[{name,input}]}` (→ tool_use blocks, stop_reason tool_use) or the answer text (→ one text block,
end_turn) → `POST /ai/relay/push?id=…` `{message, model}` (or `{skip:true}` to hand the turn to the cloud, or
`{error}`). Lessons from the debrief's chat poller: a headless session gets ONE turn — the prompt forbids "I'll check
back"; `--strict-mcp-config` + no setting sources keep MCP notices out of the answer; `--tools ""` because the browser runs the tools.
