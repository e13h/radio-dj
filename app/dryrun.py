"""Dry run: write a dozen DJ breaks for a station without going on air.

Uses the real library, play counts and shuffle, and Claude (when
ANTHROPIC_API_KEY is set), but no TTS, no downloads and no Liquidsoap. Nothing
is written to the station's history. Run it through ../dryrun.sh.

Tune `talk:` (lengths, segments), personas or pronunciations in stations.yaml,
then rerun with the same --seed: the songs stay the same and only the scripts
change.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import random
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

from config import RadioConfig
from station import WORDS_PER_SECOND, Station
from store import Store
from subsonic import Subsonic
from writer import Writer

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "stations.example.yaml"
DEMO_HISTORY = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "radio-dj-demo/data/radio.db"


def local_timezone() -> str:
    try:
        return re.sub(r".*/zoneinfo/", "", os.readlink("/etc/localtime"))
    except OSError:
        return "UTC"


def load(path: Path) -> RadioConfig:
    raw = path.read_text()
    # The repo copy still has Ansible variables; fill in what matters here.
    # (No literal double braces in this file: it passes through Jinja on deploy.)
    raw = re.sub(r"\{\{\s*timezone\s*\}\}", local_timezone(), raw)
    raw = re.sub(r"\{\{[^}]*\}\}", "example", raw)
    return RadioConfig.model_validate(yaml.safe_load(raw))


def song_line(s: dict | None) -> str:
    if not s:
        return "(nothing: station sign-on)"
    extra = ", ".join(str(x) for x in (s.get("album"), s.get("year")) if x)
    plays = s.get("playCount") or 0
    return f"{s.get('title')} - {s.get('artist')}" + (f" ({extra})" if extra else "") + f"  [{plays} plays]"


def wrap(text: str, width: int = 88, indent: str = "    ") -> str:
    words, lines, line = text.split(), [], ""
    for w in words:
        if line and len(line) + 1 + len(w) > width - len(indent):
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    return "\n".join(indent + l for l in lines)


async def run(args: argparse.Namespace) -> None:
    cfg = load(args.config)
    if args.station not in cfg.stations:
        sys.exit(f"unknown station {args.station!r}; stations.yaml has {', '.join(cfg.stations)}")

    url = os.environ.get("NAVIDROME_URL", "http://xwing.local:4533")
    user, password = os.environ.get("NAVIDROME_USER"), os.environ.get("NAVIDROME_PASSWORD")
    if not (user and password):
        sys.exit("set NAVIDROME_USER and NAVIDROME_PASSWORD (the account the radio uses)")
    radio = Subsonic(url, user, password)
    library_client = radio
    if os.environ.get("NAVIDROME_STATS_USER"):
        library_client = Subsonic(url, os.environ["NAVIDROME_STATS_USER"], os.environ["NAVIDROME_STATS_PASSWORD"])

    work = Path(tempfile.mkdtemp(prefix="radio-dryrun-"))
    try:
        # Work on a copy of the history so the dry run never changes it.
        db = work / "radio.db"
        if args.history:
            if not args.history.exists():
                sys.exit(f"no history at {args.history}")
            shutil.copy(args.history, db)
        writer = Writer(cfg.llm)
        st = Station(args.station, cfg, radio, library_client, Store(db), writer, {}, work / "media")
        seed = args.seed if args.seed is not None else random.randrange(10_000)
        st.rng = random.Random(seed)
        st.talk_rng = random.Random(f"{seed}-talk")
        await st.refresh_library()

        # Simulated clock: each break airs after the previous song finishes.
        clock = dt.datetime.now(st.tz)
        st._eta = lambda prev: clock  # type: ignore[method-assign]

        talk = st.st.talk
        print(f"{st.st.name}: {len(st.songs)} songs, seed {seed} (rerun with --seed {seed} for the same songs)")
        print(f"DJ scripts: {'Claude ' + cfg.llm.model if writer.active() else 'template (no ANTHROPIC_API_KEY)'}")
        if talk.lengths:
            tiers = ", ".join(
                f"{n} {l.words}w x{l.weight:g}" + (f" (<= {l.extras} extras)" if l.extras is not None else "")
                for n, l in talk.lengths.items()
            )
            print(f"Lengths: {tiers}")
        else:
            print(f"Lengths: every break max {talk.max_words} words")
        print()

        prev, first, slot = None, True, 0
        if not args.sign_on:
            # Join mid-show: one song has already played, so every break is a regular one.
            prev, _ = st._next_song()
            st.store.record_play(st.sid, prev)
            st.tracks_since_talk = 1
            clock += dt.timedelta(seconds=float(prev.get("duration") or 210))
            first = False
        if args.deep_dive is not None and st.request_deep_dive(args.deep_dive or None) is None:
            sys.exit(f"no artist named {args.deep_dive!r} in this library")
        lengths: Counter[str] = Counter()
        word_counts: list[int] = []
        while slot < args.count:
            song, phase = st._next_song()
            if args.deep_dive is not None and slot == 0 and phase != "deep_dive_start":
                print(
                    f"(No deep dive: no artist has {st.st.deep_dive.min_tracks}+ tracks"
                    f"{' matching ' + repr(args.deep_dive) if args.deep_dive else ''} that isn't in its cooldown.)\n"
                )
            if st._should_talk(prev, phase, first):
                slot += 1
                dj_id, req, text = await st.write_break(prev, song, "sign_on" if first else phase)
                dj = cfg.djs[dj_id]
                st.on_air_dj = dj_id
                st.store.record_break(st.sid, dj_id, text)
                st.tracks_since_talk = 0
                words = len(text.split())
                lengths[req.length or "-"] += 1
                word_counts.append(words)

                label = " | ".join(
                    x
                    for x in (
                        req.kind,
                        f"{req.length} (max {req.max_words})" if req.length else f"max {req.max_words}",
                        ", ".join(req.include) or "no extras",
                        clock.strftime("%a %-I:%M %p"),
                    )
                    if x
                )
                print(f"#{slot:<2} {label}")
                print(f"    after:  {song_line(prev)}")
                print(f"    before: {song_line(song)}")
                print(f"    {dj.name}, {words} words (~{words / WORDS_PER_SECOND:.0f}s):")
                print(wrap(text))
                spoken = st.spoken_text(text, dj)
                if spoken != text:
                    print("    spoken as:")
                    print(wrap(spoken))
                print()
                clock += dt.timedelta(seconds=words / WORDS_PER_SECOND)
            # The song "plays".
            st.store.record_play(st.sid, song)
            st.tracks_since_talk += 1
            clock += dt.timedelta(seconds=float(song.get("duration") or 210))
            prev, first = song, False

        avg = sum(word_counts) / len(word_counts)
        mix = ", ".join(f"{n} {c}" for n, c in lengths.most_common())
        print(f"{len(word_counts)} breaks: {min(word_counts)}-{max(word_counts)} words, average {avg:.0f}. Lengths: {mix}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("station", help="station id from stations.yaml, e.g. main or kids")
    p.add_argument("-n", "--count", type=int, default=12, help="number of breaks to write (default 12)")
    p.add_argument("--seed", type=int, help="replay the same songs as an earlier run")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="stations.yaml (default: the repo example)")
    p.add_argument(
        "--history",
        type=Path,
        nargs="?",
        const=DEMO_HISTORY,
        help="start from a copy of a radio.db, so rotation and 'recent breaks' match it "
        "(no path: the local demo's history)",
    )
    p.add_argument(
        "--deep-dive", nargs="?", const="", metavar="ARTIST", help="start with an artist deep dive (no name: random)"
    )
    p.add_argument("--sign-on", action="store_true", help="start with the station's sign-on break")
    p.add_argument("-v", "--verbose", action="store_true", help="show service logs")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
