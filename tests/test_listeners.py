"""Breaks are only written while someone is tuned in."""

import asyncio
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from listeners import Listeners, parse_status  # noqa: E402
from station import Station  # noqa: E402


def test_parse_status_one_or_many_sources():
    one = {"icestats": {"source": {"listenurl": "http://h:8000/main.mp3", "listeners": 2}}}
    many = {"icestats": {"source": [
        {"listenurl": "http://h:8000/kids.mp3", "listeners": 0},
        {"listenurl": "http://h:8000/main.mp3", "listeners": 3},
    ]}}
    assert parse_status(one) == {"main": 2}
    assert parse_status(many) == {"kids": 0, "main": 3}
    assert parse_status({"icestats": {}}) == {}  # nothing streaming yet


class FakeListeners(Listeners):
    def __init__(self, counts):
        super().__init__("http://unused")
        self.counts = counts

    async def _fetch(self):
        return self.counts


def fake_station(listeners):
    return SimpleNamespace(
        sid="main", listeners=listeners, audience=True, on_air_dj="sunny",
        st=SimpleNamespace(dj_at=lambda when: "cosmo"), _eta=lambda prev: dt.datetime.now(),
    )


def anyone(fake):
    return asyncio.run(Station._anyone_listening(fake, None))


def test_breaks_follow_listeners():
    assert anyone(fake_station(FakeListeners({"main": 1}))) is True
    fake = fake_station(FakeListeners({"main": 0, "kids": 4}))
    assert anyone(fake) is False
    assert fake.on_air_dj == "cosmo"  # no stale handoff when someone tunes in later
    assert anyone(fake_station(FakeListeners({}))) is False  # mount not up


def test_unreachable_icecast_means_no_breaks():
    assert anyone(fake_station(FakeListeners(None))) is False
    assert anyone(fake_station(None)) is True  # listener checks turned off


def test_items_are_prepared_when_liquidsoap_asks(monkeypatch):
    """A break and its song are prepared together, one at a time, only once the last is taken."""
    import station as station_mod

    st = object.__new__(Station)
    st.sid, st.pending, st._preparing, st.calls = "main", station_mod.deque(), None, 0

    async def prepare_next():
        st.calls += 1
        if st.calls == 2:
            raise RuntimeError("navidrome hiccup")
        await asyncio.sleep(0.01)
        st.pending += [f"break{st.calls}", f"song{st.calls}"]

    st._prepare_next = prepare_next
    monkeypatch.setattr(station_mod, "RETRY_SECONDS", 0.01)

    async def run():
        # Two requests at once (Liquidsoap retrying) share one preparation.
        a, b = await asyncio.gather(st.next_item(5), st.next_item(5))
        assert (a, b) == ("break1", "song1") and st.calls == 1
        # Nothing is prepared ahead: the next request prepares, after one failed attempt.
        assert await st.next_item(5) == "break3"
        assert st.calls == 3 and list(st.pending) == ["song3"]

    asyncio.run(run())
