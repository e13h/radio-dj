"""Writes what the DJ says between tracks, using Claude."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import anthropic

from config import DJConfig, LLMConfig, StationConfig

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# After the API rejects the key, wait this long before trying Claude again.
AUTH_RETRY_SECONDS = 3600
# Drafting left in the reply ("Count: that's 30 words. Trimming: ..."), which TTS would read out.
META_TEXT = re.compile(
    r"\b(?:\d+\s+words|word\s+(?:count|limit)|(?:over|under)\s+the\s+limit"
    r"|(?:count|trimm(?:ed|ing)|revised|revision|draft|shorter|final(?:\s+version)?)\s*:)",
    re.I,
)


@dataclass
class BreakRequest:
    """Everything the DJ needs to know to write one break."""

    kind: str  # normal | sign_on | deep_dive_start | deep_dive_track | deep_dive_end
    max_words: int = 60
    length: str = ""  # name of the rolled length tier, e.g. "short"
    just_played: dict | None = None
    up_next: dict | None = None
    include: list[str] = field(default_factory=list)
    air_time: str = ""
    handoff_from: str | None = None  # previous DJ's name when a shift changes
    artist_notes: dict[str, str] = field(default_factory=dict)  # artist -> bio excerpt
    stats: dict | None = None
    deep_dive: dict | None = None  # artist, track_number, total_tracks
    recent_breaks: list[str] = field(default_factory=list)


def song_brief(s: dict | None) -> dict | None:
    if not s:
        return None
    keys = ("title", "artist", "album", "year", "genre")
    return {k: s[k] for k in keys if s.get(k)}


INSTRUCTIONS = {
    "back_announce": "Say what just played (title and artist).",
    "coming_up": "Tease what's coming up next (title and artist).",
    "fun_fact": "Share one short, interesting fact about the artist or song, from the notes if possible.",
    "stats": (
        "Mention something from the listening stats (top songs, heavy rotation, top artists, "
        "or songs never played yet). These are this household's own plays, not global charts."
    ),
    "time_check": "Give a casual time check based on approximate_air_time, rounded loosely (\"coming up on half past one\").",
    "station_id": "Say the station name.",
}

KIND_INSTRUCTIONS = {
    "sign_on": "The station is just starting up. Welcome listeners and introduce the first song.",
    "deep_dive_start": (
        "Announce an artist deep dive: we're about to play every track in the library by this "
        "artist, in order. Give a little background on the artist from the notes, then introduce "
        "the first track."
    ),
    "deep_dive_track": (
        "We're in the middle of an artist deep dive. Connect the track that just played to the "
        "next one and drop a bit of the artist's story along the way."
    ),
    "deep_dive_end": (
        "The artist deep dive just wrapped up. Close it out briefly, then go back to "
        "regular programming with the next song."
    ),
}


class Writer:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.client: anthropic.AsyncAnthropic | None = None
        self.disabled_until = 0.0
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            self.client = anthropic.AsyncAnthropic()
            log.info("DJ scripts: Claude (%s)", cfg.model)
        else:
            log.info("DJ scripts: no ANTHROPIC_API_KEY set, using template breaks")

    def active(self) -> bool:
        """True when breaks are being written by Claude rather than the template."""
        return self.client is not None and time.time() >= self.disabled_until

    def system_prompt(self, dj: DJConfig, station: StationConfig) -> str:
        return f"""You are {dj.name}, a radio DJ on "{station.name}", a private station that plays music from one household's personal music library. {station.description}

The audience is {station.audience}. Keep everything appropriate for them.

Your persona:
{dj.persona.strip()}

How you talk:
- Write exactly the words you will say on air. They go straight to text-to-speech, so no stage directions, sound effects, emoji, markdown, or lists.
- Reply with only the final script, once. Don't count words, show a draft, or revise in your reply: everything you write is read out.
- Write things the way they are spoken: "featuring" rather than "ft.", and spell out symbols.
- Stay under the word limit you're given. Short and punchy beats long.
- Only state facts that appear in the provided notes, or that you are certain are true and widely known. If you're not sure, skip the fact and talk about the music instead. Never invent dates, chart positions, awards, or quotes.
- Don't reuse jokes, facts, or phrasings from your recent breaks.
- Breaks are recorded a few minutes before they air. If you mention the time, day or time of day, go by approximate_air_time, not when you're writing. It's an estimate, so keep times loose ("just before half past one", "around one thirty") rather than exact to the minute.
- Play counts and stats come from this household's own listening, never from global charts; describe them that way."""

    def user_prompt(self, req: BreakRequest, station: StationConfig) -> str:
        tasks = []
        if req.kind in KIND_INSTRUCTIONS:
            tasks.append(KIND_INSTRUCTIONS[req.kind])
        if req.handoff_from:
            tasks.append(f"You're just starting your shift, taking over from {req.handoff_from}. Say hello.")
        tasks += [INSTRUCTIONS[i] for i in req.include if i in INSTRUCTIONS]

        context = {
            "approximate_air_time": req.air_time,
            "just_played": song_brief(req.just_played),
            "up_next": song_brief(req.up_next),
            "deep_dive": req.deep_dive,
            "artist_notes": req.artist_notes or None,
            "listening_stats": req.stats,
            "your_recent_breaks": req.recent_breaks or None,
        }
        context = {k: v for k, v in context.items() if v}
        task_lines = "\n".join(f"- {t}" for t in tasks)
        return (
            f"Write your next break between songs.\n\n"
            f"Do this:\n{task_lines}\n\n"
            f"Word limit: {req.max_words} words"
            f"{' (a deep dive intro may use up to double)' if req.kind == 'deep_dive_start' else ''}. "
            f"Treat it as a ceiling, not a target: a short break should feel quick.\n\n"
            f"Context:\n{json.dumps(context, indent=2, ensure_ascii=False)}"
        )

    async def write(self, req: BreakRequest, dj: DJConfig, station: StationConfig) -> str:
        if not self.active():
            return template_break(req, station)
        try:
            system, prompt = self.system_prompt(dj, station), self.user_prompt(req, station)
            for attempt in (1, 2):
                text = await self._ask(system, prompt)
                if not text:
                    break
                text = clean_for_speech(text)
                if not META_TEXT.search(text):
                    return text
                log.warning("Break had drafting notes in it (attempt %d), discarding: %s", attempt, text)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self.disabled_until = time.time() + AUTH_RETRY_SECONDS
            log.warning(
                "Anthropic API key rejected (%s); using template breaks for the next %d minutes",
                e.message,
                AUTH_RETRY_SECONDS // 60,
            )
        except Exception as e:  # never let a script failure take the station off air
            log.warning("Claude request failed (%s); using template break", e)
        return template_break(req, station)

    async def _ask(self, system: str, prompt: str) -> str | None:
        assert self.client is not None
        kwargs = dict(
            model=self.cfg.model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.cfg.effort},
        )
        if self.cfg.fallbacks:
            try:
                resp = await self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            except anthropic.BadRequestError as e:
                # e.g. a model without server-side fallback support
                log.warning("Request with fallbacks rejected (%s); retrying without", e.message)
                resp = await self.client.messages.create(**kwargs)
        else:
            resp = await self.client.messages.create(**kwargs)

        if resp.stop_reason == "refusal":
            log.warning("Claude declined the break (request %s)", resp._request_id)
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_#`]+", "", text)  # markdown
    text = re.sub(r"\[[^\]]*\]|\([^)]*(?:music|laughs?|pause|sfx)[^)]*\)", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip().strip('"')


def template_break(req: BreakRequest, station: StationConfig) -> str:
    """Used when Claude is unavailable, so the station keeps talking."""
    parts = []
    if req.just_played:
        parts.append(f"That was {req.just_played.get('title')} by {req.just_played.get('artist')}.")
    if req.up_next:
        parts.append(f"Here's {req.up_next.get('title')} by {req.up_next.get('artist')}.")
    parts.append(f"You're listening to {station.name}.")
    return " ".join(parts)
