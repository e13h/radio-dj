"""Station configuration (stations.yaml) models.

NOTE: every file in this directory is rendered through Ansible's Jinja
templating on deploy, so avoid double curly braces and brace-hash pairs.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class LLMConfig(BaseModel):
    model: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Server-side refusal fallback. Only some models support it (not Haiku).
    fallbacks: bool = False


class TTSProviderConfig(BaseModel):
    # "openai" = any OpenAI-compatible /v1/audio/speech endpoint
    # (Kokoro-FastAPI locally, OpenAI in the cloud, ...).
    type: Literal["openai", "elevenlabs"]
    base_url: str | None = None
    model: str | None = None
    # Name of the environment variable holding the API key (never the key itself).
    api_key_env: str | None = None
    speed: float | None = None
    # How this provider takes pronunciation fixes: "kokoro" understands exact
    # phonemes ([word](/phonemes/)); "text" only gets sound-alike spellings.
    pronunciation: Literal["kokoro", "text"] = "text"


class TTSConfig(BaseModel):
    default_provider: str
    providers: dict[str, TTSProviderConfig]


class DJConfig(BaseModel):
    name: str
    persona: str
    voice: str
    tts: str | None = None  # provider name; defaults to tts.default_provider
    speed: float | None = None  # speaking rate, 1.0 = normal (overrides the provider's speed)
    # Loudness nudge applied by Liquidsoap to this DJ's voice, e.g. "-2 dB".
    gain: str | None = None


class Shift(BaseModel):
    dj: str
    days: list[str] = Field(default_factory=lambda: list(DAYS))
    start: dt.time = dt.time(0, 0)
    end: dt.time = dt.time(0, 0)  # end <= start means the shift wraps past midnight

    @model_validator(mode="after")
    def _check_days(self) -> "Shift":
        bad = [d for d in self.days if d not in DAYS]
        if bad:
            raise ValueError(f"unknown day(s) {bad}; use {DAYS}")
        return self

    def covers(self, when: dt.datetime) -> bool:
        t = when.time()
        today = DAYS[when.weekday()]
        if self.start < self.end:
            return today in self.days and self.start <= t < self.end
        # Wrapping shift (or 24h when start == end): the part after midnight
        # belongs to the day the shift started.
        if t >= self.start:
            return today in self.days
        yesterday = DAYS[(when.weekday() - 1) % 7]
        return t < self.end and yesterday in self.days


class Segments(BaseModel):
    """Probability (0-1) that each optional element is included in a break."""

    coming_up: float = 0.5
    fun_fact: float = 0.35
    stats: float = 0.08
    time_check: float = 0.15
    station_id: float = 0.15


class BreakLength(BaseModel):
    words: int = Field(gt=0)  # word limit given to the writer
    weight: float = Field(default=1.0, ge=0)  # relative odds of this length
    # Most optional extras (coming up, fun fact, stats, ...) a break of this
    # length may include. None = no cap.
    extras: int | None = Field(default=None, ge=0)


class TalkConfig(BaseModel):
    every_n_tracks: int = 1
    # Used when `lengths` is empty: every break gets this word limit.
    max_words: int = 60
    # Each break rolls one length by weight, e.g. short/medium/long.
    lengths: dict[str, BreakLength] = Field(default_factory=dict)
    # Seconds the DJ's voice overlaps the music on either side of a break.
    # 0 = back to back (the DJ never talks over a song); up to 3 = DJ talks
    # over the end of the outgoing song and the intro of the next one.
    overlap_seconds: float = Field(default=0.0, ge=0.0, le=3.0)
    segments: Segments = Field(default_factory=Segments)


class RotationConfig(BaseModel):
    # Share of picks drawn from your most-played songs ("heavy rotation"),
    # so favourites still come around even though rotation favours the unplayed.
    favorites_share: float = 0.15
    favorites_pool: float = 0.1  # top fraction of the library by play count
    # Don't repeat an artist within this many tracks (when the library allows).
    artist_separation: int = 4
    # Fraction of the library that must play before a track may repeat.
    repeat_window: float = 0.5


class DeepDiveConfig(BaseModel):
    enabled: bool = True
    chance_per_track: float = 0.02
    min_hours_between: float = 4
    min_tracks: int = 5
    max_tracks: int = 15
    # Don't revisit the same artist for this many days.
    artist_cooldown_days: float = 30


class StationConfig(BaseModel):
    name: str
    description: str = ""
    library: str  # Navidrome library name
    audience: str = "a general adult audience"
    shuffle: Literal["random", "rotation"] = "rotation"
    default_dj: str
    schedule: list[Shift] = Field(default_factory=list)
    talk: TalkConfig = Field(default_factory=TalkConfig)
    rotation: RotationConfig = Field(default_factory=RotationConfig)
    deep_dive: DeepDiveConfig = Field(default_factory=DeepDiveConfig)
    scrobble: bool = False

    def dj_at(self, when: dt.datetime) -> str:
        for shift in self.schedule:
            if shift.covers(when):
                return shift.dj
        return self.default_dj


class Pronunciation(BaseModel):
    phonemes: str | None = None  # Kokoro phonemes, without the slashes
    say: str | None = None  # sound-alike spelling, for any voice

    @model_validator(mode="after")
    def _need_one(self) -> "Pronunciation":
        if not (self.phonemes or self.say):
            raise ValueError("a pronunciation needs phonemes or say")
        if self.phonemes:
            self.phonemes = self.phonemes.strip().strip("/")
        return self


class RadioConfig(BaseModel):
    timezone: str = "UTC"
    public_url: str = ""
    stream_url: str = ""
    library_refresh_minutes: int = 30
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig
    djs: dict[str, DJConfig]
    stations: dict[str, StationConfig]
    # Name -> fix. "/phonemes/" (Kokoro), a sound-alike spelling, or both:
    # {phonemes: "...", say: "..."}. Matched as whole words, ignoring case.
    pronunciations: dict[str, Pronunciation] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _expand_pronunciations(cls, data: dict) -> dict:
        prons = (data or {}).get("pronunciations") or {}
        out = {}
        for name, fix in prons.items():
            if isinstance(fix, str):
                fix = fix.strip()
                is_phonemes = len(fix) > 2 and fix.startswith("/") and fix.endswith("/")
                fix = {"phonemes": fix[1:-1]} if is_phonemes else {"say": fix}
            out[str(name)] = fix
        return {**data, "pronunciations": out}

    @model_validator(mode="after")
    def _check_refs(self) -> "RadioConfig":
        for sid, st in self.stations.items():
            for dj_id in [st.default_dj] + [s.dj for s in st.schedule]:
                if dj_id not in self.djs:
                    raise ValueError(f"station {sid!r} references unknown dj {dj_id!r}")
        for dj_id, dj in self.djs.items():
            provider = dj.tts or self.tts.default_provider
            if provider not in self.tts.providers:
                raise ValueError(f"dj {dj_id!r} uses unknown tts provider {provider!r}")
        return self


def load_config(path: str | Path) -> RadioConfig:
    with open(path) as f:
        return RadioConfig.model_validate(yaml.safe_load(f))
