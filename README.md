# THAI ⇄ VIET WebTalk

Real-time Thai ↔ Vietnamese speech translation for video calls. Two people open
the same room, each picks their own language, and every utterance shows up
translated on the other side in about a second.

> Product/architecture decisions live in **[DEVELOPER_CONTEXT.md](DEVELOPER_CONTEXT.md)**.
> Read that first — this file is only "how to run it".

## Quick start (offline, no API keys)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r backend/requirements.txt
copy .env.example .env
uvicorn backend.main:app --reload --port 8000
```

Open <http://localhost:8000>, keep the room id, pick **ฉันพูดภาษาไทย**, press
**Join**, then **Start mic**. Open a second browser window with the same
`?room=` URL, pick **Tôi nói tiếng Việt**, and speak.

With `STT_PROVIDER=mock` / `TRANSLATE_PROVIDER=passthrough` you get placeholder
transcripts (`[mock th final · 2.4s]` → `[vi] [mock th final · 2.4s]`). That is
the expected offline behaviour: it proves routing, rooms, and the audio path
without spending money.

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

Programmatic deploy with the Render API is documented in
[DEVELOPER_CONTEXT.md §8.3](DEVELOPER_CONTEXT.md).

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
