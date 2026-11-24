"""radio-dj: the brains of the AI radio stations.

Liquidsoap asks GET /api/stations/<id>/next for the next file to play and
reports POST /api/stations/<id>/aired when it goes on air. Everything else
(homepage, .pls playlists, status, deep-dive trigger) is for humans.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from config import load_config
from listeners import Listeners
from station import Station
from store import Store
from subsonic import Subsonic
from tts import build_providers
from writer import Writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("radio")

CONFIG_PATH = os.environ.get("RADIO_CONFIG", "/config/stations.yaml")
DATA_DIR = Path(os.environ.get("RADIO_DATA", "/data"))
MEDIA_DIR = Path(os.environ.get("RADIO_MEDIA", "/media"))
NEXT_TIMEOUT = 240.0  # stays below Liquidsoap's 300s http timeout
MEDIA_MAX_AGE = 3 * 3600
# Debug mode: only play the first N seconds of each song (unset = full songs).
PREVIEW_SECONDS = float(os.environ.get("RADIO_PREVIEW_SECONDS") or 0) or None
# DJ breaks are only written while a station has listeners here. Set empty to always talk.
ICECAST_STATUS_URL = os.environ.get("ICECAST_STATUS_URL", "http://radio-icecast:8000/status-json.xsl")

cfg = load_config(CONFIG_PATH)
stations: dict[str, Station] = {}


async def _cleanup_media() -> None:
    """Delete prepared audio once it is safely in the past."""
    while True:
        cutoff = time.time() - MEDIA_MAX_AGE
        for f in MEDIA_DIR.glob("*/*/*"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except FileNotFoundError:
                pass
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = os.environ["NAVIDROME_URL"]
    radio = Subsonic(url, os.environ["NAVIDROME_USER"], os.environ["NAVIDROME_PASSWORD"])
    library_client = radio
    if os.environ.get("NAVIDROME_STATS_USER"):
        library_client = Subsonic(
            url, os.environ["NAVIDROME_STATS_USER"], os.environ["NAVIDROME_STATS_PASSWORD"]
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(DATA_DIR / "radio.db")
    writer = Writer(cfg.llm)
    tts = build_providers(cfg.tts.providers)
    listeners = Listeners(ICECAST_STATUS_URL) if ICECAST_STATUS_URL else None

    for sid in cfg.stations:
        stations[sid] = Station(
            sid, cfg, radio, library_client, store, writer, tts, MEDIA_DIR / sid, PREVIEW_SECONDS, listeners
        )
        stations[sid].start()
    if PREVIEW_SECONDS:
        log.warning("Preview mode: songs are cut to %gs", PREVIEW_SECONDS)
    cleanup = asyncio.create_task(_cleanup_media())
    yield
    cleanup.cancel()
    for st in stations.values():
        await st.stop()


app = FastAPI(title="AI Radio", lifespan=lifespan)


def _station(sid: str) -> Station:
    if sid not in stations:
        raise HTTPException(404, f"unknown station {sid!r}")
    return stations[sid]


def _stream_url(sid: str) -> str:
    return f"{cfg.stream_url.rstrip('/')}/{sid}.mp3"


# ---- Liquidsoap API --------------------------------------------------------


@app.get("/api/stations/{sid}/next", response_class=PlainTextResponse)
async def next_item(sid: str) -> PlainTextResponse:
    item = await _station(sid).next_item(NEXT_TIMEOUT)
    if item is None:
        return PlainTextResponse("", status_code=503)
    return PlainTextResponse(item.uri())


@app.post("/api/stations/{sid}/aired")
async def aired(sid: str, item: str) -> dict:
    await _station(sid).aired(item)
    return {"ok": True}


# ---- Human-facing API ------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/stations")
async def list_stations() -> list[dict]:
    return [st.status() for st in stations.values()]


@app.get("/api/stations/{sid}")
async def station_status(sid: str) -> dict:
    return _station(sid).status() | {"stream_url": _stream_url(sid)}


@app.post("/api/stations/{sid}/deep-dive")
async def deep_dive(sid: str, artist: str | None = None) -> dict:
    plan = _station(sid).request_deep_dive(artist)
    if plan is None:
        raise HTTPException(404, f"no artist named {artist!r} in this library")
    return {"queued": plan, "note": "starts after the track that's already lined up"}


@app.get("/{sid}.pls")
async def pls(sid: str) -> Response:
    st = _station(sid)
    body = (
        "[playlist]\n"
        f"File1={_stream_url(sid)}\n"
        f"Title1={st.st.name}\n"
        "Length1=-1\n"
        "NumberOfEntries=1\n"
        "Version=2\n"
    )
    return Response(body, media_type="audio/x-scpls")


# ---- DJ booth: try personas and voices off air ---------------------------

SEGMENTS = ["back_announce", "coming_up", "fun_fact", "stats", "time_check", "station_id"]
KINDS = ["normal", "sign_on", "deep_dive_start", "deep_dive_track", "deep_dive_end"]


class BoothRequest(BaseModel):
    station: str
    dj: str
    persona: str | None = None
    voice: str | None = None
    speed: float | None = Field(default=None, gt=0.2, le=4.0)
    kind: str = "normal"
    include: list[str] | None = None  # None = roll the station's segment odds
    text: str | None = None  # speak this instead of asking the writer


@app.get("/api/booth/config")
async def booth_config() -> dict:
    return {
        "stations": {
            sid: {"name": st.st.name, "on_air": st.st.dj_at(dt.datetime.now(st.tz))}
            for sid, st in stations.items()
        },
        "djs": {k: d.model_dump() for k, d in cfg.djs.items()},
        "segments": SEGMENTS,
        "kinds": KINDS,
        "claude": next(iter(stations.values())).writer.active() if stations else False,
    }


@app.get("/api/booth/voices")
async def booth_voices(dj: str) -> list[str]:
    """Voices offered by the DJ's TTS provider (OpenAI-compatible servers that list them)."""
    if dj not in cfg.djs:
        raise HTTPException(404, f"unknown DJ {dj!r}")
    provider = cfg.tts.providers[cfg.djs[dj].tts or cfg.tts.default_provider]
    if provider.type != "openai" or not provider.base_url:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"{provider.base_url.rstrip('/')}/audio/voices")
            r.raise_for_status()
            voices = r.json().get("voices", [])
    except Exception:
        return []
    return sorted(v if isinstance(v, str) else v.get("id", "") for v in voices)


@app.get("/api/booth/phonemize")
async def booth_phonemize(dj: str, text: str, voice: str | None = None) -> dict:
    """How Kokoro reads some text, as phonemes to paste into `pronunciations`."""
    if dj not in cfg.djs:
        raise HTTPException(404, f"unknown DJ {dj!r}")
    provider = cfg.tts.providers[cfg.djs[dj].tts or cfg.tts.default_provider]
    if provider.pronunciation != "kokoro" or not provider.base_url:
        raise HTTPException(400, "this DJ's voice doesn't use Kokoro phonemes; use a sound-alike spelling")
    root = re.sub(r"/v1/?$", "", provider.base_url.rstrip("/"))
    lang = (voice or cfg.djs[dj].voice)[:1]  # Kokoro voices start with their language: a = US, b = UK, ...
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(f"{root}/dev/phonemize", json={"text": text, "language": lang})
            r.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"Kokoro phonemize failed: {e}")
    return {"text": text, "phonemes": r.json().get("phonemes", "")}


@app.post("/api/booth")
async def booth(req: BoothRequest) -> dict:
    st = _station(req.station)
    if req.dj not in cfg.djs:
        raise HTTPException(404, f"unknown DJ {req.dj!r}")
    if req.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    changes = {k: v for k, v in {"persona": req.persona, "voice": req.voice, "speed": req.speed}.items() if v}
    dj = cfg.djs[req.dj].model_copy(update=changes)
    try:
        if req.text:
            started = time.monotonic()
            path = await st.speak(req.text.strip(), dj)
            return {
                "text": req.text.strip(),
                "spoken_as": st.spoken_text(req.text.strip(), dj),
                "words": len(req.text.split()),
                "audio": f"/api/booth/audio/{st.sid}/{path.name}",
                "source": "typed",
                "seconds": {"speak": round(time.monotonic() - started, 1)},
            }
        return await st.audition(req.dj, dj, req.kind, req.include)
    except Exception as e:
        log.exception("booth request failed")
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/api/booth/audio/{sid}/{name}")
async def booth_audio(sid: str, name: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{32}\.mp3", name):
        raise HTTPException(404)
    path = _station(sid).voice_dir / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


# ---- Pages ------------------------------------------------------------------
# Plain string templates with __PLACEHOLDERS__ (this file passes through Jinja
# on deploy, so no template engine syntax here).

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
  body { max-width: 40rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  .card { border: 1px solid #8884; border-radius: 12px; padding: 1rem 1.25rem; margin: 1rem 0; }
  .muted { opacity: .7; font-size: .9em; }
  audio { width: 100%; margin: .5rem 0; }
  ol { padding-left: 1.25rem; }
  .booth label { display: block; margin: .5rem 0; }
  .booth label.check { display: inline-block; margin: 0 .8rem 0 0; }
  .booth input, .booth select, .booth textarea { font: inherit; width: 100%; box-sizing: border-box; }
  .booth label.check input { width: auto; }
  .pickers { display: flex; gap: .5rem; } .pickers select { flex: 1; }
  fieldset { border: 1px solid #8884; border-radius: 8px; }
  pre { white-space: pre-wrap; font-size: .85em; }
  a.button { display: inline-block; padding: .3rem .8rem; border: 1px solid #8886; border-radius: 8px; text-decoration: none; }
</style></head><body>
__BODY__
</body></html>"""

STATION_BODY = """<p><a href="/">&larr; all stations</a></p>
<h1>__NAME__</h1>
<p class="muted">__DESCRIPTION__</p>
<audio controls preload="none" src="__STREAM__"></audio>
<p><a class="button" href="/__ID__.pls">.pls playlist</a> <span class="muted">Stream: __STREAM__</span></p>
<div class="card"><div class="muted">On air: <span id="dj"></span></div>
<h2 id="np">Loading...</h2><div id="said" class="muted"></div><div id="dive" class="muted"></div></div>
<h3>Recently played</h3><ol id="recent"></ol>
<script>
async function refresh() {
  const s = await (await fetch("/api/stations/__ID__")).json();
  const np = s.now_playing;
  document.getElementById("dj").textContent = s.dj;
  document.getElementById("np").textContent = np ? (np.kind === "voice" ? np.title : np.title + " — " + np.artist) : "Starting up...";
  document.getElementById("said").textContent = np && np.kind === "voice" ? "“" + np.text + "”" : (np && np.album ? np.album : "");
  document.getElementById("dive").textContent = s.deep_dive ? "Deep dive: " + s.deep_dive.artist + " (" + s.deep_dive.track_number + "/" + s.deep_dive.total_tracks + ")" : "";
  const ol = document.getElementById("recent");
  ol.replaceChildren(...s.recent.map(r => { const li = document.createElement("li"); li.textContent = r.title + " — " + r.artist; return li; }));
}
refresh(); setInterval(refresh, 10000);
</script>"""


BOOTH_BODY = r"""<p><a href="/">&larr; all stations</a></p>
<h1>DJ booth</h1>
<p class="muted">Try personas, voices and speeds off air. Each take writes a break about two
random songs from the station's library and speaks it. Nothing here changes the stations:
when you like a take, copy its settings into stations.yaml.</p>
<div class="card booth">
  <label>Station <select id="station"></select></label>
  <label>DJ <select id="dj"></select></label>
  <label>Voice <input id="voice" autocomplete="off" spellcheck="false" placeholder="af_heart, af_heart+af_bella, af_heart(2)+af_bella(1)"></label>
  <div class="pickers"><select id="voiceuse"></select> <select id="voiceblend"></select></div>
  <p class="muted">Type any voice or blend. <code>a+b</code> mixes voices equally; <code>a(2)+b(1)</code> weights them.</p>
  <label>Speed <input id="speed" type="number" step="0.05" min="0.5" max="2" placeholder="1.0"></label>
  <label>Break <select id="kind"></select></label>
  <fieldset><legend>Include <span class="muted">(none ticked = roll the station's odds)</span></legend><span id="segments"></span></fieldset>
  <label>Persona <textarea id="persona" rows="5"></textarea></label>
  <label>Or speak this exact text <span class="muted">(skips the writer; for voice and pronunciation tuning)</span>
    <textarea id="text" rows="2"></textarea></label>
  <p class="muted">Pronunciation fixes are written <code>[Name](/phonemes/)</code> here, or as
    <code>Name: /phonemes/</code> in stations.yaml.
    <button id="phon" type="button">How does Kokoro say it?</button> <span id="phonout"></span></p>
  <p><button id="go">Write &amp; speak</button> <button id="again" title="Same settings, new songs">Another take</button>
  <span id="status" class="muted"></span></p>
  <p id="writer" class="muted"></p>
</div>
<div id="takes"></div>
<script>
const $ = id => document.getElementById(id);
let conf;
function opt(sel, value, label) { const o = document.createElement("option"); o.value = value; o.textContent = label || value; sel.append(o); }
async function loadVoices() {
  const vs = await (await fetch("/api/booth/voices?dj=" + encodeURIComponent($("dj").value))).json();
  $("voiceuse").replaceChildren(); opt($("voiceuse"), "", vs.length ? "Switch to voice..." : "(no voice list)");
  $("voiceblend").replaceChildren(); opt($("voiceblend"), "", "Blend in voice...");
  vs.forEach(v => { opt($("voiceuse"), v); opt($("voiceblend"), v); });
}
function useVoice() { if ($("voiceuse").value) $("voice").value = $("voiceuse").value; $("voiceuse").value = ""; }
function blendVoice() {
  const v = $("voiceblend").value; $("voiceblend").value = "";
  if (v) $("voice").value = $("voice").value.trim() ? $("voice").value.trim() + "+" + v : v;
}
function loadDj() {
  const d = conf.djs[$("dj").value];
  $("voice").value = d.voice; $("speed").value = d.speed || ""; $("persona").value = d.persona.trim();
  loadVoices();
}
function pickStation() {
  const on = conf.stations[$("station").value].on_air;
  if (on) $("dj").value = on;
  loadDj();
}
async function init() {
  conf = await (await fetch("/api/booth/config")).json();
  for (const [sid, st] of Object.entries(conf.stations)) opt($("station"), sid, st.name);
  for (const [id, d] of Object.entries(conf.djs)) opt($("dj"), id, d.name);
  conf.kinds.forEach(k => opt($("kind"), k));
  conf.segments.forEach(seg => {
    const l = document.createElement("label"); l.className = "check";
    l.innerHTML = '<input type="checkbox" value="' + seg + '"> ' + seg; $("segments").append(l);
  });
  $("writer").textContent = conf.claude ? "Breaks are written by Claude." : "No working ANTHROPIC_API_KEY: breaks use the template, so persona edits won't change the words.";
  $("station").onchange = pickStation; $("dj").onchange = loadDj;
  $("voiceuse").onchange = useVoice; $("voiceblend").onchange = blendVoice;
  $("go").onclick = take; $("again").onclick = take; $("phon").onclick = phonemize;
  pickStation();
}
function yamlFor(dj) {
  const d = conf.djs[dj];
  const lines = ["  " + dj + ":", "    name: " + d.name, "    voice: " + $("voice").value];
  if ($("speed").value) lines.push("    speed: " + $("speed").value);
  if (d.tts) lines.push("    tts: " + d.tts);
  if (d.gain) lines.push("    gain: \"" + d.gain + "\"");
  lines.push("    persona: |");
  $("persona").value.trim().split("\n").forEach(l => lines.push("      " + l));
  return lines.join("\n");
}
async function phonemize() {
  const text = $("text").value.trim() || prompt("Name or phrase to look up:");
  if (!text) return;
  $("phonout").textContent = "...";
  const q = "dj=" + encodeURIComponent($("dj").value) + "&voice=" + encodeURIComponent($("voice").value.trim()) +
    "&text=" + encodeURIComponent(text);
  const r = await fetch("/api/booth/phonemize?" + q); const t = await r.json();
  $("phonout").textContent = r.ok ? text + " = /" + t.phonemes + "/" : "Failed: " + t.detail;
}
async function take() {
  const include = [...document.querySelectorAll("#segments input:checked")].map(i => i.value);
  const body = {
    station: $("station").value, dj: $("dj").value, kind: $("kind").value,
    persona: $("persona").value, voice: $("voice").value,
    speed: $("speed").value ? Number($("speed").value) : null,
    include: include.length ? include : null, text: $("text").value || null,
  };
  $("go").disabled = $("again").disabled = true; $("status").textContent = "Working...";
  try {
    const r = await fetch("/api/booth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const t = await r.json();
    if (!r.ok) throw new Error(t.detail || r.status);
    const card = document.createElement("div"); card.className = "card";
    const meta = [conf.djs[body.dj].name, body.voice + (body.speed ? " @ " + body.speed + "x" : ""), body.kind,
      t.source, t.words + " words" + (t.length ? " / " + t.length : "")];
    if (t.include) meta.push(t.include.join(", ") || "no extras");
    if (t.seconds) meta.push(Object.entries(t.seconds).map(([k, v]) => k + " " + v + "s").join(", "));
    card.innerHTML = '<div class="muted meta"></div><audio controls autoplay></audio><p class="said"></p><p class="muted spoken"></p><div class="muted songs"></div>' +
      '<details><summary class="muted">stations.yaml for this take</summary><pre></pre></details>';
    card.querySelector(".meta").textContent = meta.join(" · ");
    card.querySelector("audio").src = t.audio;
    card.querySelector(".said").textContent = t.text;
    if (t.spoken_as && t.spoken_as !== t.text) card.querySelector(".spoken").textContent = "Spoken as: " + t.spoken_as;
    card.querySelector(".songs").textContent = t.up_next ? (t.just_played ? "After " + t.just_played + ". " : "") + "Before " + t.up_next + "." : "";
    card.querySelector("pre").textContent = yamlFor(body.dj);
    $("takes").prepend(card); $("status").textContent = "";
  } catch (e) { $("status").textContent = "Failed: " + e.message; }
  $("go").disabled = $("again").disabled = false;
}
init();
</script>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.replace("__TITLE__", html.escape(title)).replace("__BODY__", body))


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    cards = "".join(
        f'<div class="card"><h2><a href="/stations/{sid}">{html.escape(st.st.name)}</a></h2>'
        f'<p class="muted">{html.escape(st.st.description)}</p>'
        f'<a class="button" href="/{sid}.pls">.pls</a> <span class="muted">{_stream_url(sid)}</span></div>'
        for sid, st in stations.items()
    )
    return _page("AI Radio", "<h1>AI Radio</h1>" + cards + '<p><a href="/booth">DJ booth</a> <span class="muted">try personas and voices</span></p>')


@app.get("/stations/{sid}", response_class=HTMLResponse)
async def station_page(sid: str) -> HTMLResponse:
    st = _station(sid)
    body = (
        STATION_BODY.replace("__NAME__", html.escape(st.st.name))
        .replace("__DESCRIPTION__", html.escape(st.st.description))
        .replace("__STREAM__", html.escape(_stream_url(sid)))
        .replace("__ID__", sid)
    )
    return _page(st.st.name, body)


@app.get("/booth", response_class=HTMLResponse)
async def booth_page() -> HTMLResponse:
    return _page("DJ booth", BOOTH_BODY)
