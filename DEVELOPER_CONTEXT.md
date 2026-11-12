# DEVELOPER_CONTEXT.md

**Project:** THAI_VIET_WEBTALK — Real-time Thai ↔ Vietnamese speech translator for video calls
**Hosting target:** Render (Web Service, free tier for MVP)
**Repo status:** Pre-implementation (Phase 0 — scaffolding not started)
**Last updated:** 2026-11-12
**Audience:** human developers + AI coding agents

---

## 0. How to use this file

This is the single source of truth. Read it fully before writing any code.

Rules:

1. **Never commit secrets.** API keys live in Render Environment Variables locally in `.env` only.
   `render_api.txt` on the desktop contains a Render API token — it must **never** be copied into this repo.
2. **Stay inside MVP scope** (§2). New ideas go to §14, not into the code.
3. **Locked decisions in §3 are not up for re-debate** unless a blocker is found. If you must deviate, append to the decision log in §3.3 and note it here.
4. **Update this file** whenever behaviour, endpoints, env vars, or the message protocol change.
5. Prefer a working 1-second-latency demo over a clever architecture.

### AI agent quickstart (build order)

| # | Task | Output |
|---|------|--------|
| 1 | Scaffold `backend/main.py` FastAPI app + `/healthz` | boots with `uvicorn main:app` |
| 2 | Static frontend mount at `/` | `frontend/index.html` served by FastAPI |
| 3 | WebSocket `/ws/{room_id}/{client_id}` with room registry | two clients see each other join |
| 4 | Browser mic capture → binary chunks over WS | server logs chunk sizes |
| 5 | STT adapter (`adapters/stt/base.py`) + Soniox impl | transcript in server logs |
| 6 | Translation adapter (TH↔VI) | bilingual text in logs |
| 7 | Broadcast `partial` / `final` messages | live transcript in both browsers |
| 8 | Language selection UI (speaker picks TH or VI) | correct direction per client |
| 9 | Deploy to Render via API (§8.3) | public HTTPS URL works |
| 10 | Keep-alive cron + README | no cold-start surprise |

---

## 1. Product summary

Thai and Vietnamese speakers join the same web room from a browser. The browser grabs the microphone, streams audio to the backend, and the backend turns speech into text, translates it, and pushes the translated text (and optionally synthesized speech) to the other participant in near real time.

**Primary use cases**

- **A. Companion mode** — the two people are already on Zoom / Google Meet / LINE and open this tool in a second tab to read each other's translations.
- **B. Standalone mode** — the web room *is* the meeting surface (transcript only; no video in MVP).

**Success criteria for MVP**

- Two people, one Thai-speaking and one Vietnamese-speaking, hold a 10-minute conversation.
- End-to-end text latency p50 < 1.5 s, p95 < 3 s.
- Translation is understandable (not necessarily perfect) for both sides.
- Runs on Render free tier without manual intervention beyond keep-alive.

---

## 2. Scope

### In scope (MVP)

- Bidirectional TH → VI and VI → TH text translation.
- Continuous streaming (voice-activity based utterance detection, no push-to-talk requirement).
- Live bilingual transcript panel, one column per language.
- Room model: join by URL `/room/{room_id}`, 2–6 participants.
- Language per participant (auto-detected default + manual override).
- Optional TTS playback (Phase 3, `edge-tts`).
- Render deployment, HTTPS/WSS, environment-variable secrets.

### Out of scope (explicitly)

- Video conferencing (we do not rebuild Zoom; no WebRTC video).
- Offline / on-device models.
- Speaker diarization, meeting summaries, transcript export beyond copy-to-clipboard.
- User accounts, billing, persistent storage.
- Perfect accuracy on heavy accents or code-switching.

---

## 3. Locked technical decisions

### 3.1 Stack

| Layer | Decision | Why |
|-------|----------|-----|
| Backend | **Python 3.11 + FastAPI + uvicorn** | one process serves API, WS, and static frontend |
| Realtime | **Native WebSockets** (`fastapi.WebSocket`) | Render supports WS natively; no extra infra |
| Frontend | **Vanilla HTML/CSS/JS, no build step** | zero Node build on Render, fast cold start, easy debugging |
| Audio capture | **AudioContext + ScriptProcessor → mono PCM16LE @ 16 kHz, 200 ms frames** | every STT vendor accepts raw PCM; MediaRecorder webm fragments lose their container header after the first chunk |
| STT | **Browser Web Speech API** by default (`STT_PROVIDER=browser`, free, no key); Soniox realtime behind the same adapter interface for server-side audio | deletes STT cost and upload; Soniox stays for browsers without Web Speech |
| Translation | **Lingva** (key-free Google proxy) with MyMemory fallback and a **circuit breaker** (2 failed attempts → host skipped for 10 min) | zero cost; production logs showed public Lingva hosts 403/500 constantly, so blind retries had to be capped |
| TTS | **`edge-tts`** (free, good th-VN voices) | optional, Phase 3 |
| State | **In-memory dicts** (`rooms`, `clients`) | no DB in MVP; single instance only |
| Config | `pydantic-settings` reading env vars | fail fast on missing keys |

> **Single-instance constraint:** in-memory room state means the Render service must run **exactly one instance** (`numInstances: 1`). Scaling out requires Redis pub/sub — Phase 4.

### 3.2 Language / direction rules

- Each client declares `lang` ∈ `{th, vi, en}` at join time.
- A server-side `final` transcript is translated **from the speaker's `lang` to the target language of every other client in the room**.
- Mixed rooms (3+ TH and VI speakers) are supported by the same rule — no pairwise rooms needed.
- Thai has no word spacing and Vietnamese has 6 tones + diacritics: never strip diacritics, never re-tokenize Thai output, always send UTF-8 and render with a font stack that includes `Noto Sans Thai` and `Noto Sans Vietnamese`.

### 3.3 Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-11-12 | FastAPI + vanilla JS, single service | fewest moving parts on Render free tier |
| 2026-11-12 | Adapter pattern for STT + translation | providers/pricing change fast; avoid rewrite |
| 2026-11-12 | No DB in MVP | removes spin-down/persistence issues |
| 2026-11-12 | Start with Soniox | native Thai↔Vietnamese realtime support |
| 2026-11-12 | Raw PCM16 instead of MediaRecorder opus | webm chunks after the first one have no container header, which most STT APIs cannot parse |
| 2026-11-12 | Default to browser STT + Lingva | $0, no keys, no audio upload; Soniox/LLM remain one env var away |
| 2026-11-12 | Circuit breaker on public MT hosts | 2026-11-12 Render logs: lingva.ml 403, lingva.garudalinux.org 403, translate.plausibility.cloud 500 — every phrase burned 3 requests (~2.5 s) before MyMemory answered |

---

## 4. Architecture

```
 Browser A (Thai)                        Browser B (Vietnamese)
   │ mic → MediaRecorder                    │ mic → MediaRecorder
   │                                        │
   └──── WSS /ws/{room}/{client} ───┐   ┌────┘
                                    ▼   ▼
                        ┌──────────────────────────┐
                        │  Render Web Service      │
                        │  FastAPI + uvicorn       │
                        │                          │
                        │  rooms.py   (registry)   │
                        │  ws.py      (handler)    │
                        │  adapters/stt/*          │
                        │  adapters/translate/*    │
                        │  adapters/tts/* (P3)     │
                        └───────────┬──────────────┘
                                    │ HTTPS / WS
                        ┌───────────▼──────────────┐
                        │ Soniox (STT)  +  LLM/DE  │
                        └──────────────────────────┘
```

### 4.1 Components

| Module | Responsibility |
|--------|----------------|
| `backend/main.py` | app factory, static mount, `/healthz`, `/api/rooms`, WS route |
| `backend/ws.py` | connection lifecycle, message dispatch, broadcast helpers |
| `backend/rooms.py` | `Room` / `Participant` dataclasses, join/leave, room GC |
| `backend/pipeline.py` | per-participant audio queue → STT → translate → broadcast orchestration |
| `backend/adapters/stt/base.py` | `STTStream` interface (`push(bytes)`, `on_partial`, `on_final`) |
| `backend/adapters/stt/soniox.py` | Soniox realtime client |
| `backend/adapters/translate/base.py` | `translate(text, src, tgt) -> str` |
| `backend/adapters/tts/base.py` | `synthesize(text, lang) -> bytes` (Phase 3) |
| `frontend/app.js` | mic capture, WS client, transcript rendering |

### 4.2 Single-utterance sequence

1. Client captures a 200 ms mono PCM16 frame (16 kHz) and sends it as a WS **binary** frame.
2. `ws.py` hands the frame to that participant's `TranscriptPipeline`.
3. `pipeline.py` forwards it to the STT session.
4. STT emits `partial` → server broadcasts to all other clients in room (no translation, or cheap incremental translation if budget allows).
5. STT emits `final` → server translates once per distinct target language (cache per room+text).
6. Server broadcasts `final` with original + translations.
7. Client appends to its transcript column; optional TTS plays audio.

---

## 5. WebSocket protocol (v1)

**Endpoint:** `wss://<host>/ws/{room_id}/{client_id}`

- Binary frames = raw audio, mono **PCM16LE @ 16 kHz** (200 ms per frame), no JSON wrapper.
- Text frames = JSON control/result messages.
- Server never echoes a client's own speech back to it as translated text (it may echo `partial` for its own confirmation, per `echo_self` flag).
- A client may reuse its `client_id`; on reconnect the stale connection is evicted and the new one takes over.

### 5.1 Client → Server (JSON)

```json
{ "type": "join", "lang": "th", "echo_self": true }
{ "type": "set_lang", "lang": "vi" }
{ "type": "transcript", "text": "xin chào", "is_final": true }
{ "type": "end_utterance" }
{ "type": "ping", "t": 1731400000000 }
{ "type": "leave" }```

### 5.2 Server → Client (JSON)

```json
{
  "type": "joined",
  "client_id": "c_1a2b",
  "room_id": "abc123",
  "lang": "th",
  "sample_rate": 16000,
  "stt": "browser",
  "targets": ["th", "vi", "en"],
  "participants": [{ "client_id": "c_9f8e", "lang": "vi" }]
}
```

```json
{
  "type": "partial",
  "from": "c_9f8e",
  "src_lang": "vi",
  "text": "xin chào các bạn"
}
```

```json
{
  "type": "final",
  "id": "u_0042",
  "from": "c_9f8e",
  "src_lang": "vi",
  "text": "Xin chào các bạn.",
  "translations": { "th": "สวัสดีทุกคน", "en": "Hello everyone." },
  "latency_ms": 1180
}
```

```json
{ "type": "peer_joined", "client_id": "c_7c3d", "lang": "vi" }
{ "type": "peer_left",   "client_id": "c_7c3d" }
{ "type": "peer_lang",   "client_id": "c_7c3d", "lang": "th" }
{ "type": "lang_set",    "lang": "vi" }
{ "type": "pong", "t": 1731400000000 }
{ "type": "error", "code": "stt_unavailable", "message": "STT provider unreachable" }
```

**Error codes:** `bad_message`, `room_full`, `room_limit`, `stt_unavailable`, `translate_failed`, `rate_limited`, `internal`.

### 5.3 Limits

| Item | Value |
|------|-------|
| Max clients per room | 6 |
| Max rooms (in-memory) | 100 |
| Max WS message size | 1 MB (enforced by `--ws-max-size 1048576`) |
| Max audio frame | 64 KB (larger frames dropped, logged) |
| Max control message | 8 KB of UTF-8 |
| Per-peer send timeout | 5 s, then the peer is purged |
| Transcript rate limit | 6/s sustained, burst 12 (per client) → `rate_limited`, warned once per 3 s |
| Audio rate limit | 30 frames/s, burst 60 (200 ms frames = 5/s nominal) |
| Duplicate finals | identical text within 5 s is dropped |
| Empty-room GC | 5 minutes |
| Client heartbeat | `ping` every 20 s |

---

## 6. Target repository layout

```
THAI_VIET_WEBTALK/
├── backend/
│   ├── main.py                  # FastAPI app + static mount + /healthz
│   ├── ws.py                    # WebSocket handler
│   ├── rooms.py                 # Room/Participant registry
│   ├── pipeline.py              # audio → stt → translate → broadcast
│   ├── config.py                # pydantic-settings
│   ├── adapters/
│   │   ├── factory.py           # provider selection + offline fallback
│   │   ├── stt/{base,mock,soniox}.py
│   │   └── translate/{base,passthrough,llm}.py
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── tests/                       # pytest: rooms, protocol, pipeline, config
├── pytest.ini
├── render.yaml                  # Infrastructure as code
├── .env.example                 # keys, no values
├── .gitignore                   # .env, render_api.txt, caches
├── README.md
└── DEVELOPER_CONTEXT.md         # this file
```

---

## 7. Environment variables

Set locally in `.env`, on Render in **Environment → Environment Variables**. Values never enter git.

| Variable | Required | Example | Notes |
|----------|----------|---------|-------|
| `STT_PROVIDER` | yes | `browser` | `browser` (free, client-side) \| `mock` \| `soniox` |
| `STT_API_KEY` | for soniox | *(secret)* | provider key |
| `STT_MODEL` | no | `stt-rt-v4` | provider model id |
| `TRANSLATE_PROVIDER` | yes | `lingva` | `lingva` (free) \| `passthrough` \| `llm` |
| `TRANSLATE_API_KEY` | for llm | *(secret)* | key for chosen provider |
| `TRANSLATE_MODEL` | no | `gpt-4o-mini` | OpenAI-compatible model id |
| `TRANSLATE_BASE_URL` | no | `https://api.openai.com/v1` | any `/chat/completions` gateway |
| `DEFAULT_SRC_LANG` | no | `th` | fallback source language |
| `TARGET_LANGS` | no | `th,vi,en` | languages to pre-translate |
| `MAX_ROOM_CLIENTS` | no | `6` | hard cap |
| `MAX_ROOMS` | no | `100` | in-memory room cap |
| `ROOM_TTL_SECONDS` | no | `300` | empty-room GC |
| `LOG_LEVEL` | no | `INFO` | |
| `TTS_ENABLED` | no | `false` | Phase 3 switch |
| `RENDER_API_TOKEN` | deploy-only | *(secret)* | **not** read by the app; lives outside the repo |

`.env.example` contains the same keys with empty values plus one-line comments.

---

## 8. Render deployment

### 8.1 Free tier reality check

| Fact | Impact |
|------|--------|
| Spins down after ~15 min with no inbound traffic | 30–60 s cold start kills the "just open the link" experience |
| Inbound WS traffic keeps the service awake | an open tab keeps it warm; idle rooms do not |
| 512 MB RAM, shared CPU | keep deps small; no torch, no heavy SDKs |
| 750 instance-hours/month | enough for one always-warm service (~730 h) |
| HTTPS + WSS provided automatically | required for mic permission |
| `numInstances` must be 1 | in-memory room state |

**Keep-alive strategy (MVP):** external monitor (UptimeRobot / cron-job.org) hitting `GET /healthz` every 10 minutes. Document that this consumes ~730 of the 750 free hours.

**Upgrade trigger:** switch to Starter ($7/mo) when the tool is used for real meetings — always-on removes cold starts.

### 8.2 `render.yaml` (blueprint)

```yaml
services:
  - type: web
    name: thai-viet-webtalk
    runtime: python
    plan: free
    region: singapore          # lowest RTT to TH/VN
    numInstances: 1
    buildCommand: pip install -r backend/requirements.txt
    startCommand: uvicorn backend.main:app --host 0.0.0.0 --port $PORT --ws websockets --ws-max-size 1048576
    healthCheckPath: /healthz
    autoDeploy: true
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.9
      - key: STT_PROVIDER
        value: mock            # flip to soniox once the key is in the dashboard
      - key: STT_API_KEY
        sync: false            # set in dashboard, never in git
      - key: TRANSLATE_PROVIDER
        value: passthrough     # flip to llm once the key is in the dashboard
      - key: TRANSLATE_API_KEY
        sync: false
```

> `--ws websockets` forces the `websockets` implementation instead of uvicorn's
> legacy path; `--ws-max-size` caps a frame at the documented 1 MB.

### 8.3 Deploying with the Render API

The token lives in `C:\Users\ubonp\Desktop\render_api.txt`, **outside** this
repo (it was moved out of the project root on 2026-11-12 because `.gitignore`
is no protection until the repo is initialised). It is a full-account
credential: export it into your shell, never paste it into a tracked file.

```bash
# Windows (PowerShell)
$env:RENDER_API_TOKEN = "rnd_...from ..\render_api.txt..."
# bash
export RENDER_API_TOKEN="rnd_..."
```

```bash
# 1) who am I / get ownerId
curl -s -H "Authorization: Bearer $RENDER_API_TOKEN" \
     -H "Accept: application/json" \
     https://api.render.com/v1/owners

# 2) create the web service (repo must already exist on GitHub/GitLab)
curl -s -X POST https://api.render.com/v1/services \
  -H "Authorization: Bearer $RENDER_API_TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{
        "type": "web_service",
        "name": "thai-viet-webtalk",
        "ownerId": "OWNER_ID",
        "repo": "https://github.com/USER/THAI_VIET_WEBTALK",
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
          "env": "python",
          "plan": "free",
          "region": "singapore",
          "numInstances": 1,
          "healthCheckPath": "/healthz",
          "envSpecificDetails": {
            "buildCommand": "pip install -r backend/requirements.txt",
            "startCommand": "uvicorn backend.main:app --host 0.0.0.0 --port $PORT --ws websockets"
          }
        }
      }'

# 3) trigger a deploy
curl -s -X POST https://api.render.com/v1/services/$SERVICE_ID/deploys \
  -H "Authorization: Bearer $RENDER_API_TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{"clearCache":"do_not_clear"}'

# 4) poll status
curl -s -H "Authorization: Bearer $RENDER_API_TOKEN" \
     -H "Accept: application/json" \
     https://api.render.com/v1/services/$SERVICE_ID/deploys/$DEPLOY_ID

# 5) read logs
curl -s -H "Authorization: Bearer $RENDER_API_TOKEN" \
     -H "Accept: application/json" \
     "https://api.render.com/v1/services/$SERVICE_ID/logs?limit=100"

# 6) reload / restart
curl -s -X POST https://api.render.com/v1/services/$SERVICE_ID/redeploy \
  -H "Authorization: Bearer $RENDER_API_TOKEN" -H "Accept: application/json"
```

**Security note:** the token in `render_api.txt` is a full-account credential. Treat it as compromised if it ever lands in a repo, chat log, or screenshot — rotate it at Render → Account Settings → API Keys, then update the desktop file.

### 8.4 Post-deploy checklist

- [x] `https://thai-viet-webtalk.onrender.com/healthz` returns `{"status":"ok"}` with HTTP 200
- [x] `wss://.../ws/{room}/{client}` connects from a non-browser client
- [ ] Mic permission prompt appears (HTTPS confirmed) — needs a browser
- [ ] Two tabs, opposite `lang` values, text round-trips — automated WSS test passed, human test pending
- [ ] Uptime monitor pinging `/healthz` every 10 min
- [x] Env vars set in dashboard, `.env` git-ignored, token file kept outside the repo

### 8.5 Deployment record (2026-11-12)

| Field | Value |
|-------|-------|
| Repo | `https://github.com/teerawatban-dev/thai-viet-webtalk` (public, branch `main`) |
| Service | `srv-dbqp442fngtc7389hiq0` — `thai-viet-webtalk`, free, `singapore`, 1 instance |
| URL | **https://thai-viet-webtalk.onrender.com** |
| First deploy | `dep-dbqp44tibobs73csk360` → `live` |
| Verified | `/healthz` 200 · `/` 200 (2101 B) · WSS join → `peer_joined` → `partial` → `final{th,en}` |
| Live providers | `browser` STT (Web Speech API) + `lingva` MT — **$0, no keys** |
| Verified | `/healthz` ok · WSS `transcript` → real Thai + English output (`สวัสดี ยินดีที่ได้พบคุณ` / `Hello, nice to meet you.`) |
| Next | human test in Chrome: two tabs, TH + VI |

> **Auto-deploy caveat:** this service was created from a *public Git URL*, not
> through the GitHub App, so Render receives no push webhook — `autoDeploy` has
> no effect. After every `git push`, run:
>
> ```bash
> python scripts/deploy_render.py --repo https://github.com/teerawatban-dev/thai-viet-webtalk --apply
> ```
>
> (Connect the GitHub App in the Render dashboard if you want push-to-deploy.)

---

## 9. Local development

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r backend/requirements.txt
copy .env.example .env            # then fill keys
uvicorn backend.main:app --reload --port 8000 --ws-max-size 1048576
```

Open `http://localhost:8000` (mic works on `localhost` without HTTPS).
Two windows, same `?room=xxx`, one set to Thai and one to Vietnamese.

With the defaults (`STT_PROVIDER=browser`, `TRANSLATE_PROVIDER=lingva`) you get
real Thai↔Vietnamese translation for $0: Chrome/Edge does the recognition
(Web Speech API) and posts text, and Lingva translates it server-side.
No API key is required anywhere.

For a fully offline demo set `STT_PROVIDER=mock` + `TRANSLATE_PROVIDER=passthrough`
— transcripts then read `[mock th final · 2.4s]` and translations `[vi] …`.
`/healthz` reports the active providers, and `joined.stt` tells the browser
whether to stream audio or recognise locally.

`backend/requirements.txt` (keep minimal — 512 MB budget):

```
fastapi>=0.115
uvicorn[standard]>=0.32
websockets>=13
pydantic-settings>=2.6
httpx>=0.27
```

(`python-dotenv` comes in transitively via `pydantic-settings`.)

Run the tests:

```bash
python -m pytest -q
```

---

## 10. Milestones and acceptance criteria

### Phase 1 — MVP (text only)

**Status 2026-11-12:** backend scaffold implemented and verified offline
(mock STT + passthrough MT, two-client smoke test green). Remaining: real
provider keys, browser test with two humans, Render deploy.

- [x] WS room join/leave with participant list
- [x] Mic capture + binary PCM16 frame streaming
- [x] STT adapter producing partial + final transcripts (mock implemented, Soniox wired)
- [x] TH↔VI translation of finals (passthrough implemented, OpenAI-compatible LLM wired)
- [x] Bilingual transcript UI, auto-scroll, speaker colour
- [x] `/healthz`, structured logs, graceful error messages
- [x] Public Render deploy verified (automated WSS round-trip; human test pending)
- [ ] Provider keys configured (`STT_PROVIDER=soniox`, `TRANSLATE_PROVIDER=llm`)

**Exit criteria:** 10-minute real conversation, p95 text latency < 3 s, no dropped rooms.

### Phase 2 — Usability

- [ ] Shareable room links + QR code
- [ ] Language auto-detect with confidence display
- [ ] Latency HUD (mic → display)
- [ ] Transcript copy / download (.txt, .srt)
- [ ] Reconnect with exponential backoff

### Phase 3 — Speech output

- [ ] `edge-tts` synthesis per final
- [ ] Per-speaker mute / volume
- [ ] Audio ducking so TTS doesn't fight the live call

### Phase 4 — Production

- [ ] Redis pub/sub for multi-instance
- [ ] Optional auth + room passcodes
- [ ] Rate limiting and usage metering (per-client token bucket exists; add per-IP + global quotas)
- [ ] Render Starter plan, metrics, alerting

---

## 11. Testing strategy

| Level | Tool | What |
|-------|------|------|
| Unit | `pytest` | `rooms.py` lifecycle, `pipeline.py` fan-out, config parsing |
| Protocol | `pytest` + `fastapi.testclient.TestClient` (sync WS) | join → frame → partial/final broadcast contract (§5) |
| Adapter | `pytest` + `httpx.MockTransport` | STT/translate adapters with fakes (no live keys in CI) |
| Browser | manual checklist | mic permission, two-tab round trip, TH/VI font rendering |
| Load | `k6` or `websockets` script | 20 clients × 5 min, watch memory under 512 MB |

Minimum bar before deploy: `python -m pytest -q` green + manual two-tab test passed.

---

## 12. Cost model (rough, MVP)

| Item | Estimate |
|------|----------|
| Render free web service | $0 (750 h/mo) |
| **Free mode default** (browser STT + Lingva) | **$0** — no keys, no metering |
| Soniox STT (optional upgrade) | ~$0.16–0.18 / audio hour |
| Translation via LLM (optional) | ~$0.01–0.05 / 1k utterances |
| TTS (edge-tts, Phase 3) | $0 |

Continuous all-day streaming is the cost risk — add a session cap or idle auto-stop in Phase 4.

---

## 13. Risks and mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Render cold start (30–60 s) | High | keep-alive ping, and/or Starter plan |
| STT cost creep | Medium | cap session length, log audio minutes, alert |
| Latency stacking (mic buffer + STT + MT) | High | 200 ms frames, partials first, translate once per final, region `singapore` |
| Thai segmentation errors from STT | Medium | rely on provider tokenizer, never re-split Thai text |
| Vietnamese diacritics mangled | Medium | UTF-8 end-to-end, no normalization on display |
| Provider outage | Medium | adapters fall back to offline providers at startup; runtime STT failure emits `stt_unavailable` and ignores audio instead of dropping the room |
| Browser STT needs Chrome/Edge | Medium | `joined.stt` tells the client which mode to use; Firefox users are told to switch browser. Server-side Soniox remains available by flipping one env var |
| Public Lingva/MyMemory instances go down | Medium | circuit breaker skips a host after 2 failures for 10 min; MyMemory fallback; source-text degradation; `TRANSLATE_PROVIDER=llm` with a key is the quality upgrade |
| MyMemory anonymous quota (5k chars/day) | Medium | per-client transcript rate limit; `/healthz.translate_stats` exposes `fallback`/`failed` counts; LLM adapter is the paid escape hatch |
| In-memory state lost on restart | Medium | acceptable in MVP; clients auto-rejoin (same `client_id` evicts the stale socket) |
| One stuck peer stalls the room | Low | broadcasts are parallel with a 5 s per-peer timeout, dead peers are purged |
| Slow STT socket back-pressures the receive loop | Low | `feed()` awaits the provider send; if this shows up under load, add a bounded audio queue per participant |
| Free-tier 750 h exhausted by keep-alive | Low | monitor usage, document trade-off |
| Token/secret leakage | High | `.gitignore`, env vars only, token kept outside the repo, rotate on exposure |

---

## 14. Open questions

1. **Provider commitment** — Soniox trial vs Deepgram + MT: measure real TH/VI accuracy before wiring production keys.
2. **Companion vs standalone** — if companion mode dominates, add a compact overlay UI and drop room chrome.
3. **TTS voice choice** — `th-TH-PremwadeeNeural` vs `vi-VN-HoaiMyNeural` quality check with native listeners.
4. **Room naming** — random slug vs human-readable; needs shareable-link design.
5. **Persistence** — do users want transcripts after the call (needs DB/storage, breaks stateless MVP)?

---

## 15. References

- Render WebSockets: https://render.com/docs/websocket
- Render API reference: https://api-docs.render.com/reference/introduction
- Render blueprints (`render.yaml`): https://render.com/docs/blueprint-spec
- FastAPI WebSockets: https://fastapi.tiangolo.com/advanced/websockets/
- Soniox speech translation: https://soniox.com/docs
- Deepgram streaming: https://developers.deepgram.com/docs/getting-started-with-live-streaming-audio
- MDN Web Audio API: https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API

---

**Notes for future developers / AI assistants**

- Ship Phase 1 before optimising anything.
- Every new provider goes behind an adapter — no direct SDK calls from `pipeline.py`.
- Keep `pipeline.py` free of framework details so it stays unit-testable.
- Test with real Thai and Vietnamese speakers early; synthetic TTS audio hides tone problems.
- Document every deviation from this plan in §3.3.
