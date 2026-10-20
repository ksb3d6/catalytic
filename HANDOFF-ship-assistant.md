Ship the work that a cloud Claude Code session pushed to the hosting kit repo, through the normal Bull Whistle pipeline: port into the GLASS source, rebuild the GLASS file, run the 14 QA suites, bump the version tag, scrub, push the kit, deploy aibo-app, verify live.

WHERE THE WORK IS
- Repo: https://github.com/ksb3d6/catalytic (the scrubbed hosting kit), branch main.
- Commits, oldest first, all on main (the base before them is 6cdfa1e "Bull Whistle name on every visible surface + manifest (scrubbed rebuild ?v=20261020b)"):
  2e15f48  RSI: swap approach-band tints (60-70 green, 30-40 red)
  f7d6512  Bull Whistle logo: favicon, home-screen icon, and bar wordmark
  30c0198  The desk assistant: ask about the book, screen by indicator, get coached
  0e5c7ae  RSI: window = the 40-60 band plus the RSI's own travel, no centring
  710654f  Assistant: your computer first, the Anthropic API only as the fallback
  3cf1e0c  Ignore Python caches
- Nothing was deployed. The cloud session had no Railway access and could not reach bullwhistle.com. `git diff 6cdfa1e..3cf1e0c` is the whole change: 11 files, +1254/-19.

IMPORTANT: these commits were made directly in the KIT (the build output), not in the GLASS source. If the pipeline rebuilds the kit from the source, a rebuild will overwrite them. So the first job is to port every change into the source the GLASS build reads from, then rebuild and confirm the rebuilt kit still carries each marker listed below. Do not just re-push the kit over the top of a rebuild and do not deploy the kit without the rebuild and QA.

FILES CHANGED AND WHAT TO PORT (search markers are exact strings in the kit's index.html)

1. index.html — the RSI pane renderer, `window.tfRsiSVG`:
   - approach bands now `band(60,70,C_UP)+band(30,40,C_DN)` (was the reverse); comment dated 10-20.
   - window rule replaced: `lo=Math.max(0,Math.min(40,Math.floor(dlo/5)*5))`, `hi=Math.min(100,Math.max(60,Math.ceil(dhi/5)*5))` with ±2 padding, no minimum width, no centring; level lines start at `Math.ceil(lo/10)*10`. Marker: the comment beginning `10-20 (the owner: "keep at least the 40 and the 60 within frame`.

2. index.html — the logo in the bars:
   - `<head>`: three new `<link rel="icon">` lines (favicon-32.png, favicon-16.png, logo.png 192x192) before the apple-touch-icon.
   - the desktop app bar button `#aGlyph` now contains `<span class="bw-logo"></span><span class="a-word">Bull Whistle</span>`; CSS `.a-glyph` is transparent with `gap:7px`; new rules `.bw-logo{…mask:url(logo.png)…background:var(--gl-logo,#a78bfa)}` and `.a-glyph .bw-logo`.
   - the touch strip `#mStrip`: `.m-glyph` contains `<span class="bw-logo"></span>`, followed by a new `<b class="m-word">Bull Whistle</b>`; CSS `.m-glyph{background:transparent}`, `.m-glyph .bw-logo`, `.m-word`.
   - the skin rule `html[data-gl] .m-glyph{background:var(--gl-acc)…}` became `background:transparent`.
   - new theme rules just before the Light theme block: `html[data-gl]{--gl-logo:var(--gl-acc);}` and `html[data-gl="light"]{--gl-logo:#141a24;}` (ink in Light, accent everywhere else, artwork violet with the theme off).
   - the strip's context title insert: `title` is now inserted after `.m-word` (search `var word=strip.querySelector('.m-word')`).
   - in the theme module, a new `paintFavicon()` (redraws logo.png in `--gl-logo` on a 64px canvas and sets every `link[rel="icon"]`), called at the end of `applyTheme(t)`. Marker: `10-20: the favicon in the theme's logo colour`.
   - yields.html `<head>`: the same two favicon links plus apple-touch-icon.
   - new binary files: logo.png (192px, bull alone, transparent), favicon-32.png, favicon-16.png; replaced icon-180.png and icon-512.png (bull on the artwork's dark tile #100c1a). Source: the owner's "Bull Whistle Icons — Violet" sheet; the cutout is a PNG, there is no SVG.
   - sw.js: `CACHE='aibo-v4'`, CORE adds './logo.png', './favicon-32.png', './favicon-16.png'. Bump it again if the pipeline has its own cache-version convention; keep the three files in CORE.

3. index.html — the desk assistant (BWAI). One block at the end of `<body>` between the markers `<!-- BWAI:start -->` and `<!-- BWAI:end -->` (one `<script>`, ~900 lines, self-contained IIFE; it reads `DATA`, `tfParse`, `tfEma`, `tfRsi`, `window.AIBO_POS`, `window.AIBO_ACCOUNT`, `window.AIBO_LISTS`, `window.AIBO_CRYPTO_LIVE`, `window.AIBO_FUNDS_LIVE`, `window.AIBO_STATUS`, `window.MktView.market.frames`, `window.GL48.search`, and the strip's `#glIcons`; it exposes `window.AIBO_AI`). Port the block verbatim, after the skin/strip module so `#glIcons` exists.
   - Plus one line outside the block: the account module's `PREF_KEYS` gained `'aibo.ai.memory.v1'` (search `10-20: + the desk assistant's memory`). This mirrors the assistant's memory to /me/prefs.json; keep it.
   - CSS is inside the block (`#bwai-css` style injected at runtime); no stylesheet edits elsewhere. The block also hides `#aibo-live-chip` while `body.bwai-open`.

4. main.py — the Railway server (same static serving, same headers) plus:
   - `POST /ai/chat` → text/event-stream (events delta / tool / final / error); gated on a Bearer that the backend accepts (`GET {AIBO_BACKEND}/auth/me` for a session, `GET /positions.json` for the owner's token; cached 15 min; 40 calls per 10 min per token); calls `client.beta.messages.stream(model=claude-opus-5-5, thinking adaptive, output_config.effort medium or low in voice mode, fallbacks="default" with beta server-side-fallback-2026-07-01, cache_control on the system blocks)`.
   - `GET /ai/health` → {ok, configured, model, gated}; `GET /ai/spec` → the system prompt and tool definitions (for the local-model path).
   - Environment: ANTHROPIC_API_KEY (required for the assistant), optional AIBO_AI_MODEL, AIBO_AI_EFFORT, AIBO_BACKEND (default https://api.bullwhistle.com), AIBO_AI_OPEN=1 (dev only, skips the gate — never set it in production).
   - requirements.txt (new): `anthropic>=1.15,<2`. Nixpacks installs it; the server imports the SDK lazily so the static site still serves without a key.
   - If the deploy pipeline builds main.py from somewhere else, port the whole file; it is the one in the kit at commit 710654f.

5. .gitignore (new): `__pycache__/`, `*.pyc`.

STORAGE KEYS, NAMES AND ENV — DO NOT RENAME
- New localStorage keys the assistant owns: `aibo.ai.memory.v1` (also mirrored through PREF_KEYS), `aibo.ai.conv.v1`, `aibo.ai.local.v1`. Existing keys it reads but never writes: `aibo_pos_token_v1`, `aibo_pos_v1`, `aibo.acct.token`, `aibo.mk.market.v1`, `aibo.mk.candles.v1.mk` / `.mkc`, `aibo.lists.key`.
- Service worker cache name `aibo-v4` (bump forward only, never backward).
- Railway service name `aibo-app`; env names exactly as above; the endpoint paths `/ai/chat`, `/ai/health`, `/ai/spec`.
- Backend paths the assistant calls from the browser, unchanged: /trades.json, /notes.json and /me/notes.json, /market.json, /market_candles_eq.json, /market_candles_cr.json, /market_tech/<SYM>.json, /coin_tech/<SYM>.json, /auth/me, /positions.json.
- Do not touch any other storage key, service name, or the scrub list beyond adding the new files above to whatever the scrub/kit manifest needs.

PIPELINE
1. Fetch the kit repo's main and read `git diff 6cdfa1e..3cf1e0c`. Port each item above into the GLASS source. Keep the comment text (the owner's quotes and the 10-20 dates) with the code, as the file does everywhere else.
2. Rebuild the GLASS file. Confirm the rebuilt kit contains all of: `<!-- BWAI:start -->`, `.bw-logo`, `--gl-logo`, `paintFavicon`, `'aibo.ai.memory.v1'`, `band(60,70,C_UP)+band(30,40,C_DN)`, `Math.min(40,Math.floor(dlo/5)*5)`, and that `main.py` has `/ai/chat`, `/ai/health`, `/ai/spec`. Every inline script must still parse.
3. Run the 14 QA suites. Expect the strip to carry one more icon (the sparkle "Ask" button before the You button); the desktop app bar button `#aGlyph` now contains a span and a wordmark instead of the letter A; the touch strip's children are `.m-glyph`, `.m-word`, then `#glTitle`. Fix any suite that asserted the old "A" badge or the old child order; do not loosen anything else.
4. Bump the version tag, scrub, push the kit, deploy aibo-app (`railway up --service aibo-app` from the kit folder, or the pipeline's equivalent).
5. In Railway → aibo-app → Variables, add ANTHROPIC_API_KEY. Redeploy if the variable was added after the deploy.

VERIFY LIVE (https://bullwhistle.com)
- `GET /ai/health` returns `{"ok":true,"configured":true,"model":"claude-opus-5-5","gated":true}`. `GET /ai/spec` returns the prompt and 11 tools.
- The tab icon is the bull; in the Light theme it is near-black, in Violet it is violet (the link href becomes a data: URL after the theme applies). The iPhone home-screen icon is the bull on the dark tile (re-add the bookmark to see it).
- Strip (phone, iPad, themed desktop): bull, then "Bull Whistle", then the context title; the sparkle button opens "Ask Bull Whistle".
- Ask "How's my book?" on a device holding the positions token: the reply streams, the line under it reads "cloud · used: portfolio …" (or "on your computer …" when Ollama is reachable). Ask "Which of my names have daily RSI under 35 but sit above the weekly 55 EMA?" and check it ran the screen (the "used:" line shows screen).
- Memory view: after a conversation, closing the sheet and waiting ~20 s rewrites the profile ("Memory updated." note); the profile is editable; "Forget everything" clears it.
- Voice on iPhone Safari: the mic dictates; "Talk" listens, speaks the answer, listens again.
- "Your computer first" on the Mac in Chrome: with Ollama running as `OLLAMA_ORIGINS="https://bullwhistle.com" ollama serve` and a tool-capable model pulled (llama3.1 or qwen2.5), the Memory view's Test reads "ready: <model> on this computer" and replies say "on your computer". Stop Ollama: replies fall back to "cloud · computer not found".
- RSI pane in the technicals: the 30–40 band is reddish, 60–70 greenish; 40 and 60 are always in frame; the top reaches 100 only when the RSI itself went there.
- A device with neither a positions token nor a sign-in (and no Ollama) gets the note asking for Settings › Connected accounts, and `/ai/chat` answers 401 — the key must not be spendable by strangers.

If the rebuild changes element ids, class names, or the module order such that the assistant's lookups (`#glIcons`, `.gl-you`, `.appbar`, `#aPop`, `#glSearch [data-glsr]`, `#aibo-live-chip`) no longer match, fix the lookups inside the BWAI block rather than renaming the app's elements.
