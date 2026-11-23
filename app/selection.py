"""Track selection: shuffle/rotation, library stats and deep-dive picking.

Pure functions over Subsonic song dicts so they're easy to test.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections import defaultdict

from config import DeepDiveConfig, RotationConfig

DAY = 86400.0


def played_ts(song: dict) -> float | None:
    """Navidrome's `played` (last played by the API user) as a unix timestamp."""
    raw = song.get("played")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_heard(song: dict, radio_last_aired: dict[str, float]) -> float | None:
    """Most recent time the song was heard anywhere: in your clients or on the radio."""
    times = [t for t in (played_ts(song), radio_last_aired.get(song["id"])) if t]
    return max(times) if times else None


def rotation_weight(song: dict, radio_last_aired: dict[str, float], now: float) -> float:
    """Favour songs not heard in a long time, and songs with few plays.

    Never-heard songs count as a year stale. Play count is damped with a square
    root so favourites are de-emphasised but never starved.
    """
    heard = last_heard(song, radio_last_aired)
    days_since = 365.0 if heard is None else min(365.0, max(0.0, (now - heard) / DAY))
    return (1.0 + days_since) / math.sqrt(1.0 + (song.get("playCount") or 0))


def _eligible(
    songs: list[dict],
    recent_ids: list[str],
    recent_artist_ids: list[str],
    queued_ids: set[str],
    cfg: RotationConfig,
) -> list[dict]:
    """Drop recently aired songs and artists, relaxing the rules if that empties the pool."""
    window = int(len(songs) * cfg.repeat_window)
    blocked_songs = set(recent_ids[:window]) | queued_ids
    blocked_artists = set(recent_artist_ids[: cfg.artist_separation])

    pool = [s for s in songs if s["id"] not in blocked_songs and s.get("artistId") not in blocked_artists]
    if not pool:
        pool = [s for s in songs if s["id"] not in blocked_songs]
    if not pool:
        pool = [s for s in songs if s["id"] not in queued_ids] or songs
    return pool


def pick_song(
    songs: list[dict],
    *,
    mode: str,
    radio_last_aired: dict[str, float],
    recent_ids: list[str],
    recent_artist_ids: list[str],
    queued_ids: set[str],
    cfg: RotationConfig,
    now: float,
    rng: random.Random,
) -> dict:
    if not songs:
        raise ValueError("library is empty")
    pool = _eligible(songs, recent_ids, recent_artist_ids, queued_ids, cfg)

    if mode == "random":
        return rng.choice(pool)

    # Heavy rotation: sometimes pull from your most-played songs.
    if rng.random() < cfg.favorites_share:
        ranked = sorted(songs, key=lambda s: s.get("playCount") or 0, reverse=True)
        top = ranked[: max(1, int(len(ranked) * cfg.favorites_pool))]
        pool_ids = {s["id"] for s in pool}
        favourites = [s for s in top if s["id"] in pool_ids and (s.get("playCount") or 0) > 0]
        if favourites:
            return rng.choice(favourites)

    weights = [rotation_weight(s, radio_last_aired, now) for s in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def library_stats(songs: list[dict], now: float, limit: int = 5) -> dict:
    """Facts about your listening the DJ can riff on."""
    played = [s for s in songs if (s.get("playCount") or 0) > 0]
    top = sorted(played, key=lambda s: s["playCount"], reverse=True)[:limit]
    recent = [s for s in played if (played_ts(s) or 0) >= now - 30 * DAY]
    heavy = sorted(recent, key=lambda s: s["playCount"], reverse=True)[:limit]

    artist_plays: dict[str, int] = defaultdict(int)
    for s in played:
        artist_plays[s.get("artist", "?")] += s["playCount"]
    top_artists = sorted(artist_plays.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    def brief(s: dict) -> dict:
        return {"title": s.get("title"), "artist": s.get("artist"), "plays": s.get("playCount")}

    return {
        "library_size": len(songs),
        "never_played": len(songs) - len(played),
        "all_time_top_songs": [brief(s) for s in top],
        "heavy_rotation_last_30_days": [brief(s) for s in heavy],
        "top_artists_by_plays": [{"artist": a, "plays": n} for a, n in top_artists],
    }


def _track_order(s: dict) -> tuple:
    return (s.get("year") or 9999, s.get("album") or "", s.get("discNumber") or 1, s.get("track") or 0)


def pick_deep_dive(
    songs: list[dict],
    cfg: DeepDiveConfig,
    exclude_artist_ids: set[str],
    rng: random.Random,
    artist_name: str | None = None,
) -> tuple[str, str, list[dict]] | None:
    """Choose an artist and return (artist_id, artist_name, tracks in chronological order).

    With artist_name, pick that artist (case-insensitive) regardless of limits.
    """
    by_artist: dict[str, list[dict]] = defaultdict(list)
    for s in songs:
        if s.get("artistId"):
            by_artist[s["artistId"]].append(s)

    if artist_name:
        wanted = artist_name.casefold()
        matches = [aid for aid, tracks in by_artist.items() if tracks[0].get("artist", "").casefold() == wanted]
        if not matches:
            return None
        artist_id = matches[0]
    else:
        candidates = [
            aid
            for aid, tracks in by_artist.items()
            if len(tracks) >= cfg.min_tracks and aid not in exclude_artist_ids
        ]
        if not candidates:
            return None
        # Lean toward artists you actually listen to, but give everyone a shot.
        weights = [1 + math.sqrt(sum(t.get("playCount") or 0 for t in by_artist[a])) for a in candidates]
        artist_id = rng.choices(candidates, weights=weights, k=1)[0]

    tracks = by_artist[artist_id]
    if len(tracks) > cfg.max_tracks:
        tracks = rng.sample(tracks, cfg.max_tracks)
    tracks = sorted(tracks, key=_track_order)
    return artist_id, tracks[0].get("artist", "Unknown artist"), tracks
