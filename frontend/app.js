/* THAI ⇄ VIET WebTalk — browser client.
 *
 * Responsibilities:
 *   1. capture the mic as mono 16 kHz PCM16 (works with every STT provider)
 *   2. do crude voice-activity gating so we do not stream / pay for silence
 *   3. keep a WebSocket room connection alive and render the transcript
 *
 * Audio path: getUserMedia -> AudioContext -> ScriptProcessor -> downsample
 *             -> Int16 -> 200 ms frames -> WebSocket binary frames
 */

const SAMPLE_RATE = 16000;
const FRAME_SAMPLES = 3200; // 200 ms @ 16 kHz mono
const SPEECH_PEAK = 0.02; // frame considered speech above this peak
const SILENCE_HANG_MS = 700; // stop streaming after this much silence
const PING_INTERVAL_MS = 20000;
const LANGS = ["th", "vi", "en"];
const LANG_TAGS = { th: "th-TH", vi: "vi-VN", en: "en-US" };
const SpeechRecognitionImpl =
  window.SpeechRecognition || window.webkitSpeechRecognition || null;

const el = {
  room: document.getElementById("roomInput"),
  lang: document.getElementById("langSelect"),
  join: document.getElementById("joinBtn"),
  mic: document.getElementById("micBtn"),
  status: document.getElementById("status"),
  incoming: document.getElementById("incoming"),
  outgoing: document.getElementById("outgoing"),
  meta: document.getElementById("meta"),
  copy: document.getElementById("copyBtn"),
  share: document.getElementById("shareBtn"),
  peerLabel: document.getElementById("peerLabel"),
  selfLabel: document.getElementById("selfLabel"),
  modeHint: document.getElementById("modeHint"),
};

const state = {
  ws: null,
  room: null,
  clientId: randomId(8),
  lang: "th",
  joined: false,
  closing: false,
  reconnectAttempt: 0,
  reconnectTimer: null,
  pingTimer: null,
  micWanted: false,
  sttMode: "server",
  recognition: null,
  recogWanted: false,
  recogRestartTimer: null,
  audio: {
    ctx: null,
    stream: null,
    source: null,
    node: null,
    pending: [],
    pendingLen: 0,
    speaking: false,
    silentSince: 0,
    running: false,
    starting: false,
  },
  history: [],
};

/* ------------------------------------------------------------------ utils */

function randomId(n) {
  const bytes = new Uint8Array(n / 2);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, n);
}

function roomFromLocation() {
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("room");
  if (fromQuery) return sanitizeRoom(fromQuery);
  const match = location.pathname.match(/\/room\/([A-Za-z0-9_-]+)/);
  if (match) return sanitizeRoom(match[1]);
  return randomId(6);
}

function sanitizeRoom(value) {
  return String(value || "").trim().replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
}

function setStatus(text, kind = "idle") {
  el.status.textContent = text;
  el.status.className = `pill ${kind}`;
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/${encodeURIComponent(state.room)}/${state.clientId}`;
}

function sendJSON(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function saveLang(lang) {
  try {
    localStorage.setItem("tvwt.lang", lang);
  } catch {
    /* private mode / storage disabled */
  }
}

function loadLang() {
  try {
    return localStorage.getItem("tvwt.lang");
  } catch {
    return null;
  }
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }

  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/* ------------------------------------------------------------- connection */

function connect() {
  clearTimeout(state.reconnectTimer);
  state.closing = false;

  let socket;
  try {
    socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";
    state.ws = socket;
  } catch (err) {
    setStatus("ws error", "err");
    return;
  }

  socket.addEventListener("open", () => {
    if (state.ws !== socket) return;
    state.reconnectAttempt = 0;
    sendJSON({
      type: "join",
      lang: state.lang,
      sample_rate: SAMPLE_RATE,
      echo_self: true,
    });
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(
      () => sendJSON({ type: "ping", t: Date.now() }),
      PING_INTERVAL_MS,
    );
    setStatus("connected", "live");
  });

  socket.addEventListener("message", (event) => {
    if (state.ws !== socket) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    handleMessage(payload);
  });

  socket.addEventListener("close", () => {
    // Ignore the close of a socket we already replaced or abandoned.
    if (state.ws !== socket) return;

    state.joined = false;
    state.ws = null;
    clearInterval(state.pingTimer);
    el.mic.disabled = true;

    if (state.closing) {
      stopMic();
      setStatus("left", "idle");
      return;
    }

    // Keep the microphone alive across a transient drop: audio is simply
    // discarded until the socket is back (pushPcm checks readyState).
    setStatus("reconnecting…", "warn");
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    if (state.ws === socket) setStatus("socket error", "err");
  });
}

function scheduleReconnect() {
  state.reconnectAttempt += 1;
  const delay = Math.min(15000, 800 * 2 ** (state.reconnectAttempt - 1));
  setStatus(`reconnecting in ${Math.round(delay / 1000)}s`, "warn");
  state.reconnectTimer = setTimeout(connect, delay);
}

/* --------------------------------------------------------------- messages */

function handleMessage(msg) {
  switch (msg.type) {
    case "joined": {
      state.joined = true;
      el.mic.disabled = false;
      el.copy.disabled = false;
      el.share.disabled = false;
      el.join.textContent = "Leave";
      el.selfLabel.textContent = langName(state.lang);
      const peers = msg.participants || [];
      el.peerLabel.textContent = peers.length
        ? peers.map((p) => langName(p.lang)).join(", ")
        : "waiting…";
      state.sttMode = msg.stt === "browser" ? "browser" : "server";
      el.meta.textContent = `room ${msg.room_id} · you ${msg.client_id} · stt ${state.sttMode}`;
      el.modeHint.textContent =
        state.sttMode === "browser"
          ? `browser STT (${LANG_TAGS[state.lang] || "th-TH"}) · key-free translation`
          : "server STT";
      setStatus(`in room · ${peers.length + 1}`, "live");

      if (state.sttMode === "browser" && !SpeechRecognitionImpl) {
        setStatus("use Chrome/Edge", "err");
      }

      if (state.sttMode === "browser") {
        if (state.recogWanted && !state.recognition) startRecognition();
      } else if (state.micWanted && !state.audio.running) {
        startMic();
      }
      break;
    }

    case "partial": {
      if (msg.from === state.clientId) {
        renderPartial(el.outgoing, msg.text, "self");
      } else {
        el.peerLabel.textContent = langName(msg.src_lang);
        renderPartial(el.incoming, msg.text, "");
      }
      break;
    }

    case "final": {
      const isSelf = msg.from === state.clientId;
      if (isSelf) {
        renderFinal(el.outgoing, msg, true);
      } else {
        el.peerLabel.textContent = langName(msg.src_lang);
        renderFinal(el.incoming, msg, false);
      }
      break;
    }

    case "peer_joined": {
      setStatus(`peer joined (${langName(msg.lang)})`, "live");
      el.peerLabel.textContent = langName(msg.lang);
      break;
    }

    case "peer_left": {
      setStatus("peer left", "warn");
      break;
    }

    case "peer_lang": {
      el.peerLabel.textContent = langName(msg.lang);
      break;
    }

    case "lang_set": {
      state.lang = msg.lang;
      el.selfLabel.textContent = langName(msg.lang);
      break;
    }

    case "pong":
      break;

    case "error": {
      setStatus(msg.code || "error", "err");
      console.warn("server error", msg);
      break;
    }

    default:
      console.debug("unhandled message", msg);
  }
}

function langName(code) {
  return { th: "Thai", vi: "Vietnamese", en: "English" }[code] || code || "?";
}

function clearPartials(feed) {
  feed.querySelectorAll(".entry.partial").forEach((node) => node.remove());
}

function renderPartial(feed, text, extraClass) {
  clearPartials(feed);
  const node = document.createElement("div");
  node.className = `entry partial ${extraClass}`.trim();
  const body = document.createElement("div");
  body.className = "trans";
  body.textContent = text;
  node.appendChild(body);
  feed.appendChild(node);
  feed.scrollTop = feed.scrollHeight;
}

function renderFinal(feed, msg, isSelf) {
  clearPartials(feed);

  const mine = msg.translations?.[state.lang] || msg.text;
  const node = document.createElement("div");
  node.className = `entry ${isSelf ? "self" : ""}`.trim();

  if (isSelf) {
    const orig = document.createElement("div");
    orig.className = "trans";
    orig.textContent = msg.text;
    node.appendChild(orig);

    const other = Object.entries(msg.translations || {});
    if (other.length) {
      const alt = document.createElement("div");
      alt.className = "orig";
      alt.textContent = other.map(([code, text]) => `${code}: ${text}`).join("  ·  ");
      node.appendChild(alt);
    }
  } else {
    const orig = document.createElement("div");
    orig.className = "orig";
    orig.textContent = `${langName(msg.src_lang)} · ${msg.text}`;
    node.appendChild(orig);

    const trans = document.createElement("div");
    trans.className = "trans";
    trans.textContent = mine;
    node.appendChild(trans);
  }

  const meta = document.createElement("div");
  meta.className = "meta-line";
  meta.textContent = `${new Date().toLocaleTimeString()} · ${msg.latency_ms ?? 0} ms`;
  node.appendChild(meta);

  feed.appendChild(node);
  feed.scrollTop = feed.scrollHeight;

  while (feed.children.length > 300) {
    feed.firstElementChild.remove();
  }

  state.history.push({
    at: new Date().toISOString(),
    from: msg.from,
    self: isSelf,
    src_lang: msg.src_lang,
    text: msg.text,
    translations: msg.translations || {},
  });
  if (state.history.length > 500) state.history.shift();
}

/* ------------------------------------------------------------------ audio */

function peakOf(samples) {
  let peak = 0;
  for (let i = 0; i < samples.length; i += 8) {
    const value = Math.abs(samples[i]);
    if (value > peak) peak = value;
  }
  return peak;
}

function downsample(input, inRate, outRate) {
  if (inRate === outRate) return input;
  const ratio = inRate / outRate;
  const length = Math.floor(input.length / ratio);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = pos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

function pushPcm(samples) {
  const buffer = state.audio;
  buffer.pending.push(samples);
  buffer.pendingLen += samples.length;

  while (buffer.pendingLen >= FRAME_SAMPLES) {
    const out = new Int16Array(FRAME_SAMPLES);
    let filled = 0;
    while (filled < FRAME_SAMPLES) {
      const head = buffer.pending[0];
      const take = Math.min(head.length, FRAME_SAMPLES - filled);
      for (let i = 0; i < take; i += 1) {
        const clamped = Math.max(-1, Math.min(1, head[i]));
        out[filled + i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      filled += take;
      if (take === head.length) {
        buffer.pending.shift();
      } else {
        buffer.pending[0] = head.subarray(take);
      }
      buffer.pendingLen -= take;
    }
    if (
      state.ws &&
      state.ws.readyState === WebSocket.OPEN &&
      state.ws.bufferedAmount < 1_000_000
    ) {
      state.ws.send(out.buffer);
    }
  }
}

async function startMic() {
  if (state.audio.running || state.audio.starting) return;
  state.audio.starting = true;
  state.micWanted = true;

  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    if (!state.micWanted) {
      // The user pressed Stop while the permission prompt was open.
      stream.getTracks().forEach((track) => track.stop());
      return;
    }

    const ctx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
    });
    await ctx.resume();

    const source = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(2048, 1, 1);

    node.onaudioprocess = (event) => {
      const raw = event.inputBuffer.getChannelData(0);
      const pcm = downsample(raw, ctx.sampleRate, SAMPLE_RATE);
      const peak = peakOf(pcm);
      const now = performance.now();
      const audio = state.audio;

      if (peak > SPEECH_PEAK) {
        audio.speaking = true;
        audio.silentSince = 0;
        setStatus("listening", "live");
        pushPcm(pcm);
        return;
      }

      if (!audio.speaking) return;

      pushPcm(pcm); // keep the tail so the last syllable is not cut
      if (!audio.silentSince) audio.silentSince = now;
      if (now - audio.silentSince > SILENCE_HANG_MS) {
        audio.speaking = false;
        audio.silentSince = 0;
        sendJSON({ type: "end_utterance" });
        setStatus("in room · waiting for speech", "live");
      }
    };

    source.connect(node);
    node.connect(ctx.destination); // outputs silence; required in some browsers

    Object.assign(state.audio, {
      ctx,
      stream,
      source,
      node,
      pending: [],
      pendingLen: 0,
      speaking: false,
      silentSince: 0,
      running: true,
    });

    el.mic.textContent = "■ Stop mic";
    el.mic.classList.add("on");
  } catch (err) {
    console.error(err);
    state.micWanted = false;
    stream?.getTracks().forEach((track) => track.stop());
    setStatus("mic blocked", "err");
  } finally {
    state.audio.starting = false;
  }
}

function stopMic() {
  state.micWanted = false;
  const audio = state.audio;
  if (!audio.running) return;

  if (audio.speaking) sendJSON({ type: "end_utterance" });

  try {
    audio.node?.disconnect();
    audio.source?.disconnect();
  } catch {
    /* already gone */
  }
  audio.stream?.getTracks().forEach((track) => track.stop());
  audio.ctx?.close().catch(() => {});

  Object.assign(audio, {
    ctx: null,
    stream: null,
    source: null,
    node: null,
    pending: [],
    pendingLen: 0,
    speaking: false,
    running: false,
  });

  el.mic.textContent = "● Start mic";
  el.mic.classList.remove("on");
}

/* ------------------------------------------------- browser speech (free) */
/* Chrome/Edge run the recogniser and post text, so the server needs no STT
 * key and no audio upload. Firefox has no implementation. */

function startRecognition() {
  if (!SpeechRecognitionImpl) {
    setStatus("needs Chrome/Edge", "err");
    return;
  }
  if (state.recognition) return;

  state.recogWanted = true;
  const rec = new SpeechRecognitionImpl();
  rec.lang = LANG_TAGS[state.lang] || "th-TH";
  rec.continuous = true;
  rec.interimResults = true;
  rec.maxAlternatives = 1;

  rec.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i];
      const text = (result[0]?.transcript || "").trim();
      if (!text) continue;
      sendJSON({ type: "transcript", text, is_final: Boolean(result.isFinal) });
    }
  };

  rec.onerror = (event) => {
    const code = event.error || "error";
    if (code === "no-speech" || code === "aborted") return;
    if (code === "not-allowed" || code === "service-not-allowed") {
      state.recogWanted = false;
      setStatus("mic blocked", "err");
      return;
    }
    setStatus(`speech: ${code}`, "warn");
  };

  rec.onend = () => {
    state.recognition = null;
    if (state.closing) return;
    if (state.recogWanted) {
      // Chrome ends the session on silence; keep it alive.
      state.recogRestartTimer = setTimeout(() => {
        try {
          startRecognition();
        } catch {
          /* transient */
        }
      }, 300);
      return;
    }
    el.mic.textContent = "● Start mic";
    el.mic.classList.remove("on");
  };

  try {
    rec.start();
  } catch (err) {
    console.error(err);
    state.recogWanted = false;
    setStatus("speech start failed", "err");
    return;
  }

  state.recognition = rec;
  el.mic.textContent = "■ Stop mic";
  el.mic.classList.add("on");
  setStatus("listening (browser)", "live");
}

function stopRecognition() {
  state.recogWanted = false;
  clearTimeout(state.recogRestartTimer);
  const rec = state.recognition;
  state.recognition = null;
  if (rec) {
    try {
      rec.stop();
    } catch {
      /* already stopped */
    }
  }
  el.mic.textContent = "● Start mic";
  el.mic.classList.remove("on");
}

/* ------------------------------------------------------------------- boot */

function joinRoom() {
  if (state.ws) return; // already connecting or connected

  const room = sanitizeRoom(el.room.value) || roomFromLocation();
  state.room = room;
  el.room.value = room;
  state.closing = false;
  state.lang = LANGS.includes(el.lang.value) ? el.lang.value : "th";
  saveLang(state.lang);

  const url = new URL(location.href);
  url.searchParams.set("room", room);
  history.replaceState(null, "", url);

  connect();
}

function leaveRoom() {
  state.closing = true;
  clearTimeout(state.reconnectTimer);
  clearInterval(state.pingTimer);
  sendJSON({ type: "leave" });
  stopMic();
  stopRecognition();
  try {
    state.ws?.close();
  } catch {
    /* already closed */
  }
  state.ws = null;
  state.joined = false;
  el.join.textContent = "Join";
  el.mic.disabled = true;
  setStatus("left", "idle");
}

el.join.addEventListener("click", () => {
  if (state.joined || state.ws) {
    leaveRoom();
  } else {
    joinRoom();
  }
});

el.mic.addEventListener("click", () => {
  if (state.sttMode === "browser") {
    if (state.recogWanted) {
      stopRecognition();
    } else {
      startRecognition();
    }
    return;
  }
  if (state.audio.running) {
    stopMic();
  } else {
    startMic();
  }
});

el.copy.addEventListener("click", async () => {
  const lines = state.history.map((item) => {
    const who = item.self ? "ME" : "PEER";
    const trans = Object.entries(item.translations)
      .map(([code, text]) => `${code}: ${text}`)
      .join(" | ");
    return `[${item.at}] ${who} (${item.src_lang}) ${item.text}${trans ? `  ->  ${trans}` : ""}`;
  });
  const ok = await copyText(lines.join("\n"));
  el.copy.textContent = ok ? "Copied!" : "Copy failed";
  setTimeout(() => (el.copy.textContent = "Copy transcript"), 1200);
});

el.share.addEventListener("click", async () => {
  const url = new URL(location.href);
  url.searchParams.set("room", state.room || el.room.value);
  const ok = await copyText(url.toString());
  el.share.textContent = ok ? "Link copied!" : "Copy failed";
  setTimeout(() => (el.share.textContent = "Copy invite link"), 1200);
});

el.lang.addEventListener("change", () => {
  const next = LANGS.includes(el.lang.value) ? el.lang.value : "th";
  state.lang = next;
  saveLang(next);
  el.selfLabel.textContent = langName(next);
  if (state.joined) sendJSON({ type: "set_lang", lang: next });

  // Browser STT bakes the language into the recogniser: restart it.
  if (state.sttMode === "browser" && state.recogWanted) {
    const wasWanted = state.recogWanted;
    stopRecognition();
    if (wasWanted) startRecognition();
  }
});

window.addEventListener("beforeunload", () => {
  state.closing = true;
  stopMic();
  stopRecognition();
});

(function boot() {
  const savedLang = loadLang();
  if (LANGS.includes(savedLang)) {
    state.lang = savedLang;
    el.lang.value = savedLang;
  }

  state.room = roomFromLocation();
  el.room.value = state.room;
  el.selfLabel.textContent = langName(state.lang);

  el.room.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      joinRoom();
    }
  });

  setStatus("idle", "idle");
})();
