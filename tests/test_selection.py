"""Run: uv run --no-project --with-requirements app/requirements.txt --with pytest pytest tests"""

import datetime as dt
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import selection  # noqa: E402
from config import DeepDiveConfig, RotationConfig, Shift, load_config  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def song(i, artist="a", plays=0, played=None, **kw):
    s = {"id": f"s{i}", "title": f"Song {i}", "artist": f"Artist {artist}", "artistId": artist, "playCount": plays}
    if played is not None:
        s["played"] = iso(played)
    return s | kw


def pick_many(songs, n=4000, cfg=None, **kw):
    rng = random.Random(1)
    cfg = cfg or RotationConfig(favorites_share=0, artist_separation=0, repeat_window=0)
    args = dict(mode="rotation", radio_last_aired={}, recent_ids=[], recent_artist_ids=[], queued_ids=set())
    args |= kw
    return Counter(selection.pick_song(songs, cfg=cfg, now=NOW, rng=rng, **args)["id"] for _ in range(n))


def test_unplayed_songs_beat_recently_heavily_played_ones():
    songs = [song(1, plays=50, played=NOW - DAY), song(2, plays=0)]
    counts = pick_many(songs)
    assert counts["s2"] > 20 * counts["s1"]


def test_favorites_still_come_around():
    songs = [song(1, plays=50, played=NOW - DAY)] + [song(i, plays=0) for i in range(2, 50)]
    cfg = RotationConfig(favorites_share=0.15, artist_separation=0, repeat_window=0)
    counts = pick_many(songs, cfg=cfg)
    assert counts["s1"] > 0.1 * 4000  # heavy rotation share


def test_radio_plays_count_as_recently_heard():
    songs = [song(1), song(2)]
    counts = pick_many(songs, radio_last_aired={"s1": NOW - 600})
    assert counts["s2"] > 20 * counts["s1"]


def test_recent_and_queued_songs_and_artists_are_excluded():
    songs = [song(1, "a"), song(2, "b"), song(3, "c"), song(4, "a")]
    cfg = RotationConfig(favorites_share=0, artist_separation=1, repeat_window=0.5)
    counts = pick_many(songs, cfg=cfg, recent_ids=["s2", "s3"], recent_artist_ids=["a"], queued_ids={"s1"})
    # s2/s3 recently aired, artist a just played -> nothing is fully eligible;
    # relaxing artist separation leaves s4 (s1 is queued).
    assert set(counts) == {"s4"}


def test_random_mode_is_uniform_over_eligible():
    songs = [song(1, plays=99, played=NOW), song(2)]
    counts = pick_many(songs, mode="random")
    assert 0.4 < counts["s1"] / 4000 < 0.6


def test_deep_dive_orders_chronologically_and_respects_limits():
    songs = [song(i, "x", year=2000 + (5 - i), album=f"A{i}", track=1) for i in range(5)] + [song(9, "y")]
    cfg = DeepDiveConfig(min_tracks=3, max_tracks=10)
    artist_id, name, tracks = selection.pick_deep_dive(songs, cfg, set(), random.Random(0))
    assert (artist_id, name) == ("x", "Artist x")
    assert [t["year"] for t in tracks] == sorted(t["year"] for t in tracks)
    assert selection.pick_deep_dive(songs, cfg, {"x"}, random.Random(0)) is None
    assert selection.pick_deep_dive(songs, cfg, set(), random.Random(0), artist_name="artist Y")[0] == "y"


def test_stats():
    songs = [song(1, plays=10, played=NOW - DAY), song(2, plays=3, played=NOW - 90 * DAY), song(3)]
    stats = selection.library_stats(songs, NOW)
    assert stats["never_played"] == 1
    assert stats["all_time_top_songs"][0]["title"] == "Song 1"
    assert [s["title"] for s in stats["heavy_rotation_last_30_days"]] == ["Song 1"]


def test_shifts_wrap_midnight():
    night = Shift(dj="luna", days=["fri"], start=dt.time(19), end=dt.time(6))
    assert night.covers(dt.datetime(2026, 11, 20, 23))  # Friday night
    assert night.covers(dt.datetime(2026, 11, 21, 2))  # early Saturday = Friday's shift
    assert not night.covers(dt.datetime(2026, 11, 21, 23))  # Saturday night


def test_example_config_loads():
    cfg = load_config(ROOT / "stations.example.yaml")
    assert set(cfg.stations) == {"main", "kids"}
    for when in (dt.datetime(2026, 11, 17, 8), dt.datetime(2026, 11, 17, 3)):
        assert cfg.stations["main"].dj_at(when) == "sunny"
        assert cfg.stations["kids"].dj_at(when) == "cosmo"
