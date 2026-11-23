"""Break lengths, segment caps and pronunciation fixes."""

import random
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from config import Pronunciation, RadioConfig, StationConfig  # noqa: E402
from pronounce import Pronouncer  # noqa: E402
from station import Station  # noqa: E402

BASE = {
    "tts": {"default_provider": "k", "providers": {"k": {"type": "openai", "base_url": "http://x/v1"}}},
    "djs": {"d": {"name": "D", "persona": "p", "voice": "af_heart"}},
    "stations": {"s": {"name": "S", "library": "main", "default_dj": "d"}},
}


def fake_station(talk: dict, seed: int = 1) -> SimpleNamespace:
    st = StationConfig.model_validate({"name": "S", "library": "main", "default_dj": "d", "talk": talk})
    return SimpleNamespace(st=st, talk_rng=random.Random(seed))


def test_lengths_follow_weights():
    fake = fake_station({"lengths": {"short": {"words": 10, "weight": 3}, "long": {"words": 60, "weight": 1}}})
    picks = Counter(Station.pick_length(fake)[0] for _ in range(4000))
    assert 2.5 < picks["short"] / picks["long"] < 3.5


def test_no_lengths_uses_max_words():
    fake = fake_station({"max_words": 42})
    assert Station.pick_length(fake) == ("", 42, None)


def test_extras_cap():
    always = {k: 1.0 for k in ("coming_up", "fun_fact", "stats", "time_check", "station_id")}
    fake = fake_station({"segments": always})
    prev = {"title": "x"}
    assert Station.pick_segments(fake, prev, "normal", 0) == ["back_announce"]
    assert len(Station.pick_segments(fake, prev, "normal", 2)) == 3
    assert len(Station.pick_segments(fake, prev, "normal", None)) == 6
    # Required parts of special breaks survive any cap.
    assert Station.pick_segments(fake, prev, "deep_dive_start", 0) == ["back_announce", "coming_up", "fun_fact"]
    assert Station.pick_segments(fake, None, "sign_on", 0) == ["coming_up"]


def test_pronunciation_config_forms():
    cfg = RadioConfig.model_validate(
        BASE | {"pronunciations": {"Sade": "/ʃɑdˈA/", "Hozier": "HOH-zee-er", "Björk": {"phonemes": "/bjˈɜɹk/", "say": "Byerk"}}}
    )
    p = cfg.pronunciations
    assert p["Sade"] == Pronunciation(phonemes="ʃɑdˈA")
    assert p["Hozier"] == Pronunciation(say="HOH-zee-er")
    assert p["Björk"] == Pronunciation(phonemes="bjˈɜɹk", say="Byerk")


def test_pronouncer():
    pr = Pronouncer(
        {
            "Sade": Pronunciation(phonemes="ʃɑdˈA", say="Shah-day"),
            "Sigur Rós": Pronunciation(say="Sigh-ur Rose"),
            "Hozier": Pronunciation(phonemes="hˈOziəɹ"),
        }
    )
    text = "That was SADE. Sade's best, then Sigur Rós and Hozier. Crusade!"
    assert pr.apply(text, "kokoro") == (
        "That was [SADE](/ʃɑdˈA/). [Sade](/ʃɑdˈA/)'s best, then Sigh-ur Rose and [Hozier](/hˈOziəɹ/). Crusade!"
    )
    # Voices without phoneme support get the spelling, or the word unchanged.
    assert pr.apply(text, "text") == "That was Shah-day. Shah-day's best, then Sigh-ur Rose and Hozier. Crusade!"
    assert Pronouncer({}).apply(text, "kokoro") == text


def test_eta_counts_everything_ahead_of_the_break():
    """A break written while one song plays and two more are lined up airs after all of them."""
    import time
    from zoneinfo import ZoneInfo

    from station import Item

    def song(duration: float) -> Item:
        return Item(kind="song", path=Path("x"), title="t", artist="a", duration=duration)

    playing, fetched, prev = song(240), song(200), song(210)
    playing.started_at = time.time() - 60  # 180s left
    fake = SimpleNamespace(tz=ZoneInfo("UTC"), now_playing=playing, upcoming=[fetched, prev])
    eta = Station._eta(fake, {"duration": 210}).timestamp() - time.time()
    assert abs(eta - (180 + 200 + 210)) < 2



def test_drafting_notes_are_caught():
    from writer import META_TEXT

    leaked = [
        "That was Tuesdays by Jake Scott. Count: that's about 30 words, over the limit. Trimming: That was Jake Scott.",
        "Here comes Let It Go! Wait, that's 17 words. Trimmed: Next, Let It Go!",
        "Revised: That was People by Jon McLaughlin.",
    ]
    clean = [
        "That was Tuesdays by Jake Scott. Here's Allen Stone with Miscommunicate.",
        "It's coming up on half past one, and this one counts down to the weekend.",
        "Fifteen years after their first record, here's the final track from the album.",
    ]
    assert all(META_TEXT.search(t) for t in leaked)
    assert not any(META_TEXT.search(t) for t in clean)
