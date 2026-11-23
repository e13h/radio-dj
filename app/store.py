"""SQLite history: what aired, what the DJs said, which artists got deep dives."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    station TEXT NOT NULL,
    song_id TEXT NOT NULL,
    artist_id TEXT,
    artist TEXT,
    title TEXT,
    aired_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plays_station_time ON plays (station, aired_at);
CREATE TABLE IF NOT EXISTS breaks (
    station TEXT NOT NULL,
    dj TEXT NOT NULL,
    text TEXT NOT NULL,
    aired_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS breaks_station_time ON breaks (station, aired_at);
CREATE TABLE IF NOT EXISTS deep_dives (
    station TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    artist TEXT NOT NULL,
    started_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def record_play(self, station: str, song: dict) -> None:
        self.db.execute(
            "INSERT INTO plays VALUES (?, ?, ?, ?, ?, ?)",
            (station, song["id"], song.get("artistId"), song.get("artist"), song.get("title"), time.time()),
        )
        self.db.commit()

    def record_break(self, station: str, dj: str, text: str) -> None:
        self.db.execute("INSERT INTO breaks VALUES (?, ?, ?, ?)", (station, dj, text, time.time()))
        self.db.commit()

    def record_deep_dive(self, station: str, artist_id: str, artist: str) -> None:
        self.db.execute(
            "INSERT INTO deep_dives VALUES (?, ?, ?, ?)", (station, artist_id, artist, time.time())
        )
        self.db.commit()

    def recent_song_ids(self, station: str, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT song_id FROM plays WHERE station = ? ORDER BY aired_at DESC LIMIT ?",
            (station, limit),
        )
        return [r["song_id"] for r in rows]

    def recent_artist_ids(self, station: str, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT artist_id FROM plays WHERE station = ? ORDER BY aired_at DESC LIMIT ?",
            (station, limit),
        )
        return [r["artist_id"] for r in rows if r["artist_id"]]

    def last_aired(self, station: str) -> dict[str, float]:
        """song_id -> last time this station played it."""
        rows = self.db.execute(
            "SELECT song_id, MAX(aired_at) AS t FROM plays WHERE station = ? GROUP BY song_id",
            (station,),
        )
        return {r["song_id"]: r["t"] for r in rows}

    def station_top(self, station: str, since: float, limit: int = 5) -> list[dict]:
        rows = self.db.execute(
            """SELECT title, artist, COUNT(*) AS n FROM plays
               WHERE station = ? AND aired_at >= ?
               GROUP BY song_id ORDER BY n DESC LIMIT ?""",
            (station, since, limit),
        )
        return [dict(r) for r in rows]

    def recent_breaks(self, station: str, limit: int = 4) -> list[dict]:
        rows = self.db.execute(
            "SELECT dj, text, aired_at FROM breaks WHERE station = ? ORDER BY aired_at DESC LIMIT ?",
            (station, limit),
        )
        return [dict(r) for r in rows]

    def recent_plays(self, station: str, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT title, artist, aired_at FROM plays WHERE station = ? ORDER BY aired_at DESC LIMIT ?",
            (station, limit),
        )
        return [dict(r) for r in rows]

    def last_deep_dive(self, station: str) -> float | None:
        row = self.db.execute(
            "SELECT MAX(started_at) AS t FROM deep_dives WHERE station = ?", (station,)
        ).fetchone()
        return row["t"]

    def recent_deep_dive_artists(self, station: str, since: float) -> set[str]:
        rows = self.db.execute(
            "SELECT artist_id FROM deep_dives WHERE station = ? AND started_at >= ?", (station, since)
        )
        return {r["artist_id"] for r in rows}
