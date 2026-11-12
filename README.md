# THAI ⇄ VIET WebTalk

Real-time Thai ↔ Vietnamese speech translation for video calls. Two people open
the same room, each picks their own language, and every utterance shows up
translated on the other side in about a second.

> Product/architecture decisions live in **[DEVELOPER_CONTEXT.md](DEVELOPER_CONTEXT.md)**.
> Read that first — this file is only "how to run it".

## Quick start (free mode — no API keys)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r backend/requirements.txt
copy .env.example .env
uvicorn backend.main:app --reload --port 8000
```

Open <http://localhost:8000> in **Chrome or Edge**, keep the room id, pick
**ฉันพูดภาษาไทย**, press **Join**, then **Start mic**. Open a second window with
the same `?room=` URL, pick **Tôi nói tiếng Việt**, and speak.

How it works in free mode:

- the **browser** does speech recognition (Web Speech API) — no audio is uploaded
- the server translates with **Lingva** (key-free Google proxy, MyMemory fallback)
- total cost: $0, no accounts, no keys

> Firefox has no Web Speech API. Set `STT_PROVIDER=soniox` (server-side audio)
> for other browsers.

## Turn on real translation

Edit `.env`:

```ini
STT_PROVIDER=soniox
STT_API_KEY=sk_...
TRANSLATE_PROVIDER=llm
TRANSLATE_API_KEY=...
TRANSLATE_MODEL=gemini-3.1-flash-lite
TRANSLATE_BASE_URL=https://api.openai.com/v1   # or any OpenAI-compatible gateway
```

Restart the server and check `GET /healthz` — it reports the active providers.

## Endpoints

| Path | Purpose |
|------|---------|
| `GET /` , `GET /room/{room_id}` | frontend |
| `GET /healthz` | liveness + active providers (Render health check) |
| `GET /api/rooms` | in-memory room summary |
| `WS /ws/{room_id}/{client_id}` | audio in, transcripts out |

## Deploy to Render

Blueprint deploy: push the repo, then **New → Blueprint** and point it at
`render.yaml`. Secrets (`STT_API_KEY`, `TRANSLATE_API_KEY`) are `sync: false`,
so set them in the dashboard.

CLI deploy (used for this project — the service was created from a public Git
URL, so there is **no push webhook**):

```bash
python scripts/deploy_render.py --repo https://github.com/<user>/thai-viet-webtalk --apply
```

Live: <https://thai-viet-webtalk.onrender.com>

## Tests

```bash
pytest -q
```

## Notes / known limits

- Room state is in-process: run **one** instance (`numInstances: 1`).
- Free tier sleeps after ~15 min idle; a `/healthz` ping every 10 min keeps it warm.
- Browser sends mono 16 kHz PCM16, 200 ms frames, with a simple peak-based VAD
  that stops streaming during silence.
- Thai has no word spacing and Vietnamese is tonal — never strip diacritics or
  re-tokenise output.
