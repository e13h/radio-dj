"""One station: picks tracks, schedules DJ breaks and deep dives, prepares audio."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import selection
from config import DJConfig, RadioConfig, StationConfig
from listeners import Listeners
from pronounce import Pronouncer
from store import Store
from subsonic import Subsonic
from tts import TTSProvider
from writer import BreakRequest, Writer

log = logging.getLogger(__name__)

BIO_CHARS = 1500
WORDS_PER_SECOND = 2.6
TTS_ATTEMPTS = 3


def _annotate_value(v: object) -> str:
    # Liquidsoap annotate values are double-quoted; keep them simple and single-line.
    return str(v).replace('"', "”").replace("\n", " ")


@dataclass
class Item:
    kind: str  # "song" | "voice"
    path: Path
    title: str
    artist: str
    duration: float
    song: dict | None = None
    dj: str | None = None
    text: str | None = None
    gain: str | None = None
    cue_out: float | None = None  # stop playback after this many seconds
    overlap: float = 0.0  # voice only: seconds of overlap with the music around it
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float | None = None

    def uri(self) -> str:
        meta = {"title": self.title, "artist": self.artist, "radio_kind": self.kind, "radio_item": self.id}
        if self.song and self.song.get("album"):
            meta["album"] = self.song["album"]
        if self.gain:
            meta["liq_amplify"] = self.gain
        if self.cue_out:
            meta["liq_cue_out"] = f"{self.cue_out:.1f}"
        if self.kind == "voice":
            meta["radio_overlap"] = f"{self.overlap:.1f}"
        inner = ",".join(f'{k}="{_annotate_value(v)}"' for k, v in meta.items())
        return f"annotate:{inner}:{self.path}"

    def public(self) -> dict:
        d = {"kind": self.kind, "title": self.title, "artist": self.artist, "started_at": self.started_at}
        if self.song:
            d["album"] = self.song.get("album")
            d["year"] = self.song.get("year")
        if self.kind == "voice":
            d["dj"] = self.dj
            d["text"] = self.text
        return d


class Station:
    def __init__(
        self,
        sid: str,
        cfg: RadioConfig,
        radio: Subsonic,
        library_client: Subsonic,
        store: Store,
        writer: Writer,
        tts: dict[str, TTSProvider],
        media_dir: Path,
        preview_seconds: float | None = None,
        listeners: Listeners | None = None,
    ):
        self.sid = sid
        self.cfg = cfg
        self.st: StationConfig = cfg.stations[sid]
        self.radio = radio  # bot account: downloads
        self.library_client = library_client  # whose play counts/stats the station uses
        self.store = store
        self.writer = writer
        self.tts = tts
        self.tz = ZoneInfo(cfg.timezone)
        self.rng = random.Random()  # song picks
        # Talk decisions (segments, lengths) use their own generator, so a dry
        # run with a fixed seed keeps the same songs while talk knobs change.
        self.talk_rng = random.Random()
        self.pronouncer = Pronouncer(cfg.pronunciations)
        # Debug: play only the first N seconds of each song.
        self.preview_seconds = preview_seconds
        # None: always talk. Otherwise breaks are only written while someone is tuned in.
        self.listeners = listeners
        self.audience = True  # last answer, to log only when it changes

        self.track_dir = media_dir / "tracks"
        self.voice_dir = media_dir / "voice"
        self.track_dir.mkdir(parents=True, exist_ok=True)
        self.voice_dir.mkdir(parents=True, exist_ok=True)

        self.folder_id: str | None = None
        self.songs: list[dict] = []
        self.library_loaded_at = 0.0
        self.bios: dict[str, str] = {}

        self.ready: asyncio.Queue[Item] = asyncio.Queue(maxsize=1)
        # Items queued or handed to Liquidsoap but not yet on air, in play order.
        self.upcoming: list[Item] = []
        self.queued_song_ids: set[str] = set()
        self.now_playing: Item | None = None
        self.on_air_dj: str | None = None
        self.last_eta: dt.datetime | None = None  # when the last break written was expected to air
        self.tracks_since_talk = 0

        self.dive_tracks: list[dict] = []
        self.dive_info: dict | None = None
        self.dive_requested: str | None = None
        self.dive_just_ended = False
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._produce(), name=f"{self.sid}-producer"),
            asyncio.create_task(self._refresh_loop(), name=f"{self.sid}-refresh"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def _resolve_folder(self) -> str:
        folders = await self.library_client.music_folders()
        for f in folders:
            if str(f.get("name", "")).casefold() == self.st.library.casefold():
                return str(f["id"])
        names = [f.get("name") for f in folders]
        raise RuntimeError(f"library {self.st.library!r} not visible to the Navidrome user; found {names}")

    async def refresh_library(self) -> None:
        if self.folder_id is None:
            self.folder_id = await self._resolve_folder()
        songs = await self.library_client.all_songs(self.folder_id)
        if not songs:
            raise RuntimeError(f"library {self.st.library!r} returned no songs")
        self.songs = songs
        self.library_loaded_at = time.time()
        log.info("[%s] loaded %d songs from library %r", self.sid, len(songs), self.st.library)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.library_refresh_minutes * 60)
            try:
                await self.refresh_library()
            except Exception:
                log.exception("[%s] library refresh failed; keeping previous copy", self.sid)

    # ---- liquidsoap-facing -----------------------------------------------

    async def next_item(self, timeout: float) -> Item | None:
        try:
            item = await asyncio.wait_for(self.ready.get(), timeout)
        except TimeoutError:
            return None
        return item

    async def aired(self, item_id: str) -> None:
        idx = next((i for i, it in enumerate(self.upcoming) if it.id == item_id), None)
        if idx is None:
            return
        item = self.upcoming[idx]
        # Liquidsoap plays in order: anything before this one was dropped (e.g. a restart).
        del self.upcoming[: idx + 1]
        item.started_at = time.time()
        self.now_playing = item
        if item.kind == "voice":
            log.info("[%s] ON AIR %s: %s", self.sid, item.dj, item.text)
            self.store.record_break(self.sid, item.dj or "", item.text or "")
            return
        log.info("[%s] ON AIR %s - %s", self.sid, item.artist, item.title)
        assert item.song
        self.queued_song_ids.discard(item.song["id"])
        self.store.record_play(self.sid, item.song)
        if self.st.scrobble:
            try:
                await self.library_client.scrobble(item.song["id"], item.started_at)
            except Exception as e:
                log.warning("[%s] scrobble failed: %s", self.sid, e)

    def request_deep_dive(self, artist: str | None) -> dict | None:
        """Start a deep dive after the current track. Returns the plan, or None if no match."""
        if not self.songs:
            return None
        if artist:
            found = selection.pick_deep_dive(self.songs, self.st.deep_dive, set(), self.rng, artist_name=artist)
            if not found:
                return None
        self.dive_requested = artist or ""
        return {"artist": artist or "(station's choice)"}

    def status(self) -> dict:
        now = dt.datetime.now(self.tz)
        dj_id = self.st.dj_at(now)
        return {
            "id": self.sid,
            "name": self.st.name,
            "description": self.st.description,
            "library": self.st.library,
            "library_size": len(self.songs),
            "dj": self.cfg.djs[dj_id].name,
            "now_playing": self.now_playing.public() if self.now_playing else None,
            "deep_dive": self.dive_info,
            "recent": self.store.recent_plays(self.sid, 10),
        }

    # ---- programming -----------------------------------------------------

    async def _produce(self) -> None:
        prev: dict | None = None
        first = True
        while True:
            try:
                if not self.songs:
                    await self.refresh_library()
                song, phase = self._next_song()
                song_item = await self._prepare_song(song)
                if self._should_talk(prev, phase, first) and await self._anyone_listening(prev):
                    voice = await self._prepare_break(prev, song, "sign_on" if first else phase)
                    if voice:
                        self.upcoming.append(voice)
                        await self.ready.put(voice)
                        self.tracks_since_talk = 0
                self.upcoming.append(song_item)
                await self.ready.put(song_item)
                self.tracks_since_talk += 1
                prev, first = song, False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] producer error; retrying shortly", self.sid)
                await asyncio.sleep(15)

    def _next_song(self) -> tuple[dict, str]:
        """Pick the next song. The phase describes the break that precedes it."""
        if self.dive_tracks:
            song = self.dive_tracks.pop(0)
            self.dive_info["track_number"] += 1
            return song, "deep_dive_track"
        if self.dive_info:
            self.dive_info = None
            self.dive_just_ended = True
        if self._start_deep_dive():
            song = self.dive_tracks.pop(0)
            self.dive_info["track_number"] = 1
            return song, "deep_dive_start"

        phase = "deep_dive_end" if self.dive_just_ended else "normal"
        self.dive_just_ended = False
        now = time.time()
        song = selection.pick_song(
            self.songs,
            mode=self.st.shuffle,
            radio_last_aired=self.store.last_aired(self.sid),
            recent_ids=self.store.recent_song_ids(self.sid, len(self.songs)),
            recent_artist_ids=self.store.recent_artist_ids(self.sid, self.st.rotation.artist_separation),
            queued_ids=self.queued_song_ids,
            cfg=self.st.rotation,
            now=now,
            rng=self.rng,
        )
        return song, phase

    def _start_deep_dive(self) -> bool:
        dd = self.st.deep_dive
        requested, self.dive_requested = self.dive_requested, None
        if requested is None:
            if not dd.enabled or self.rng.random() >= dd.chance_per_track:
                return False
            last = self.store.last_deep_dive(self.sid)
            if last and time.time() - last < dd.min_hours_between * 3600:
                return False
        recent = self.store.recent_deep_dive_artists(self.sid, time.time() - dd.artist_cooldown_days * 86400)
        found = selection.pick_deep_dive(self.songs, dd, recent, self.rng, artist_name=requested or None)
        if not found:
            return False
        artist_id, artist, tracks = found
        self.store.record_deep_dive(self.sid, artist_id, artist)
        self.dive_tracks = list(tracks)
        self.dive_info = {"artist": artist, "artist_id": artist_id, "track_number": 0, "total_tracks": len(tracks)}
        log.info("[%s] deep dive: %s (%d tracks)", self.sid, artist, len(tracks))
        return True

    def _should_talk(self, prev: dict | None, phase: str, first: bool) -> bool:
        if first or phase != "normal":
            return True
        if self.on_air_dj and self.on_air_dj != self.st.dj_at(self._eta(prev)):
            return True  # shift change
        n = self.st.talk.every_n_tracks
        return n > 0 and self.tracks_since_talk >= n

    async def _anyone_listening(self, prev: dict | None) -> bool:
        """Skip breaks (no Claude, no TTS) while nobody is tuned in, or Icecast can't say."""
        if self.listeners is None:
            return True
        audience = await self.listeners.count(self.sid) > 0
        if audience != self.audience:
            log.info("[%s] %s", self.sid, "listeners tuned in: DJ breaks on" if audience else "no listeners: DJ breaks off")
            self.audience = audience
        if not audience:
            # Whoever tunes in later hears the DJ on shift then, without a stale handoff.
            self.on_air_dj = self.st.dj_at(self._eta(prev))
        return audience

    def _eta(self, prev: dict | None) -> dt.datetime:
        """When the next break would air: after what's playing and everything lined up behind it.

        `prev` is already in `upcoming`; the parameter is kept so dry runs can fake the clock.
        """
        now = time.time()
        seconds = sum(it.duration for it in self.upcoming)
        np = self.now_playing
        if np and np.started_at:
            seconds += max(0.0, np.started_at + np.duration - now)
        return dt.datetime.fromtimestamp(now + seconds, self.tz)

    async def _prepare_song(self, song: dict) -> Item:
        duration = float(song.get("duration") or 0)
        cue_out = None
        if self.preview_seconds and (not duration or duration > self.preview_seconds):
            cue_out = self.preview_seconds
            duration = self.preview_seconds
        item = Item(
            kind="song",
            path=self.track_dir / f"{uuid.uuid4().hex}.{song.get('suffix') or 'mp3'}",
            title=song.get("title") or "Unknown",
            artist=song.get("artist") or "Unknown",
            duration=duration,
            song=song,
            cue_out=cue_out,
        )
        await self.radio.download(song["id"], item.path)
        self.queued_song_ids.add(song["id"])
        return item

    async def _bio(self, song: dict | None) -> tuple[str, str] | None:
        if not song or not song.get("artistId"):
            return None
        aid = song["artistId"]
        if aid not in self.bios:
            self.bios[aid] = (await self.radio.artist_bio(aid))[:BIO_CHARS]
        return (song.get("artist", ""), self.bios[aid]) if self.bios[aid] else None

    def pick_length(self) -> tuple[str, int, int | None]:
        """Roll this break's length: (name, word limit, cap on extras)."""
        lengths = self.st.talk.lengths
        if not lengths:
            return "", self.st.talk.max_words, None
        names = list(lengths)
        name = self.talk_rng.choices(names, weights=[lengths[n].weight for n in names])[0]
        return name, lengths[name].words, lengths[name].extras

    def pick_segments(self, prev: dict | None, phase: str, max_extras: int | None = None) -> list[str]:
        seg = self.st.talk.segments
        include = ["back_announce"] if prev else []
        required = []
        if phase in ("sign_on", "deep_dive_start", "deep_dive_end"):
            required.append("coming_up")
        if phase.startswith("deep_dive"):
            required.append("fun_fact")
        rolled = [
            name
            for name in ("coming_up", "fun_fact", "stats", "time_check", "station_id")
            if self.talk_rng.random() < getattr(seg, name) and name not in required
        ]
        if max_extras is not None:
            self.talk_rng.shuffle(rolled)
            rolled = rolled[: max(0, max_extras - len(required))]
        order = ["back_announce", "coming_up", "fun_fact", "stats", "time_check", "station_id"]
        return [n for n in order if n in include + required + rolled]

    async def build_break_request(
        self,
        prev: dict | None,
        nxt: dict,
        phase: str,
        dj_id: str,
        include: list[str],
        when: dt.datetime,
        handoff_from: str | None = None,
        max_words: int | None = None,
        length: str = "",
    ) -> BreakRequest:
        req = BreakRequest(
            kind=phase,
            max_words=max_words or self.st.talk.max_words,
            length=length,
            just_played=prev,
            up_next=nxt,
            include=include,
            air_time=when.strftime("%A %-I:%M %p"),
            handoff_from=handoff_from,
            recent_breaks=[b["text"] for b in self.store.recent_breaks(self.sid, 4) if b["dj"] == dj_id],
        )
        if "fun_fact" in include:
            for song in (prev, nxt):
                if bio := await self._bio(song):
                    req.artist_notes[bio[0]] = bio[1]
        if "stats" in include:
            req.stats = selection.library_stats(self.songs, time.time())
            req.stats["this_station_last_7_days"] = self.store.station_top(self.sid, time.time() - 7 * 86400)
        if self.dive_info and phase.startswith("deep_dive"):
            req.deep_dive = {k: v for k, v in self.dive_info.items() if k != "artist_id"}
        return req

    def spoken_text(self, text: str, dj: DJConfig) -> str:
        """The text as sent to TTS, with pronunciation fixes applied."""
        style = self.cfg.tts.providers[dj.tts or self.cfg.tts.default_provider].pronunciation
        return self.pronouncer.apply(text, style)

    async def speak(self, text: str, dj: DJConfig) -> Path:
        """Render text in the DJ's voice, retrying a flaky TTS server."""
        path = self.voice_dir / f"{uuid.uuid4().hex}.mp3"
        provider = self.tts[dj.tts or self.cfg.tts.default_provider]
        text = self.spoken_text(text, dj)
        for attempt in range(1, TTS_ATTEMPTS + 1):
            try:
                await provider.synthesize(text, dj.voice, path, speed=dj.speed)
                return path
            except Exception as e:
                if attempt == TTS_ATTEMPTS:
                    raise
                log.warning("[%s] TTS attempt %d failed (%s); retrying", self.sid, attempt, e)
                await asyncio.sleep(10)
        raise AssertionError("unreachable")

    async def audition(self, dj_id: str, dj: DJConfig, phase: str, include: list[str] | None) -> dict:
        """DJ booth: write and voice a break about two random songs, off air.

        `dj` may be an edited copy of the configured DJ (persona, voice, speed),
        so changes can be tried before they go into stations.yaml.
        """
        if not self.songs:
            await self.refresh_library()
        prev, nxt = self.rng.sample(self.songs, 2) if len(self.songs) > 1 else (None, self.songs[0])
        if phase == "sign_on":
            prev = None
        length, words, max_extras = self.pick_length()
        if include is None:
            include = self.pick_segments(prev, phase, max_extras)
        req = await self.build_break_request(
            prev, nxt, phase, dj_id, include, dt.datetime.now(self.tz), max_words=words, length=length
        )
        if phase.startswith("deep_dive"):
            req.deep_dive = {"artist": nxt.get("artist"), "track_number": 1 if phase == "deep_dive_start" else 3,
                             "total_tracks": 8}
        started = time.monotonic()
        text = await self.writer.write(req, dj, self.st)
        wrote = time.monotonic() - started
        path = await self.speak(text, dj)
        return {
            "text": text,
            "words": len(text.split()),
            "length": f"{length} (max {words} words)" if length else f"max {words} words",
            "spoken_as": self.spoken_text(text, dj),
            "include": include,
            "just_played": f"{prev['title']} by {prev.get('artist')}" if prev else None,
            "up_next": f"{nxt['title']} by {nxt.get('artist')}",
            "audio": f"/api/booth/audio/{self.sid}/{path.name}",
            "source": "claude" if self.writer.active() else "template",
            "seconds": {"write": round(wrote, 1), "speak": round(time.monotonic() - started - wrote, 1)},
        }

    async def write_break(self, prev: dict | None, nxt: dict, phase: str) -> tuple[str, BreakRequest, str]:
        """Decide who talks, what about and for how long, and write it: (dj_id, request, text)."""
        eta = self.last_eta = self._eta(prev)
        dj_id = self.st.dj_at(eta)
        handoff = self.cfg.djs[self.on_air_dj].name if self.on_air_dj and self.on_air_dj != dj_id else None
        length, words, max_extras = self.pick_length()
        include = self.pick_segments(prev, phase, max_extras)
        req = await self.build_break_request(prev, nxt, phase, dj_id, include, eta, handoff, words, length)
        text = await self.writer.write(req, self.cfg.djs[dj_id], self.st)
        return dj_id, req, text

    async def _prepare_break(self, prev: dict | None, nxt: dict, phase: str) -> Item | None:
        dj_id, _, text = await self.write_break(prev, nxt, phase)
        dj = self.cfg.djs[dj_id]
        try:
            path = await self.speak(text, dj)
        except Exception as e:
            log.error("[%s] TTS failed %d times (%s); skipping this break", self.sid, TTS_ATTEMPTS, e)
            return None
        self.on_air_dj = dj_id
        eta = self.last_eta.strftime("%-I:%M:%S %p") if self.last_eta else "?"
        log.info("[%s] prepared break (expected on air ~%s) %s: %s", self.sid, eta, dj.name, text)
        return Item(
            kind="voice",
            path=path,
            title=f"{dj.name} on the mic",
            artist=self.st.name,
            duration=len(text.split()) / WORDS_PER_SECOND,
            dj=dj.name,
            text=text,
            gain=dj.gain,
            overlap=self.st.talk.overlap_seconds,
        )
