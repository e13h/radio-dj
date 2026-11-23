# AI Radio

AI-hosted radio stations that play from the Navidrome libraries, with a crew
of Claude-written DJs who take shifts.

| Station | Library | Stream | Homepage |
|---|---|---|---|
| Shuffle & Hum | `main` | `https://radio-stream.<tailnet>/main.mp3` | `https://radio.<tailnet>/stations/main` |
| Sprout FM | `kids` | `https://radio-stream.<tailnet>/kids.mp3` | `https://radio.<tailnet>/stations/kids` |

`.pls` playlists for Nautiline and other clients: `https://radio.<tailnet>/main.pls`, `/kids.pls`.

## How it works

```
radio-dj (Python)          picks tracks, writes breaks (Claude), renders voice (TTS),
  │                        downloads tracks from Navidrome, serves homepage/.pls
  │  GET /api/stations/<id>/next    POST /api/stations/<id>/aired
radio-liquidsoap           plays what radio-dj hands it, crossfades, ReplayGain
  │
radio-icecast              serves one mount per station
radio-kokoro               local TTS (OpenAI-compatible API)
```

- **Config:** `stations.yaml` (start from `stations.example.yaml`), mounted at
  `/config/stations.yaml` in `radio-dj`: DJs, personas, voices, shifts, how
  often they talk, rotation and deep-dive settings. Station ids must also be
  listed in `STATIONS` on `radio-liquidsoap`.
- **Shuffle:** `rotation` (default) weights each song by how long since it was
  last heard (in your clients *or* on the radio) and damps by play count, so
  unplayed and long-forgotten songs come first. `favorites_share` reserves a
  slice of picks for your most-played songs. `random` is uniform.
- **Stats:** sometimes DJs mention your all-time top songs, heavy rotation (last
  30 days), top artists, or how many songs have never been played.
- **Deep dives:** occasionally a DJ plays every track by one artist in
  chronological order, using the artist bio from Navidrome (Last.fm) for facts.
  Start one manually:
  ```bash
  curl -X POST "https://radio.<tailnet>/api/stations/main/deep-dive?artist=Radiohead"
  curl -X POST "https://radio.<tailnet>/api/stations/main/deep-dive"   # station picks
  ```
- **DJ booth:** `https://radio.<tailnet>/booth` writes and speaks test breaks
  off air. Change the station, DJ, persona, voice (Kokoro blends like
  `af_heart+af_bella` work), speed, break type and segments, and compare takes
  side by side. Each take shows the `stations.yaml` snippet for those settings.
  Nothing there changes the live stations.
- **Break lengths:** `talk.lengths` gives each station short/medium/long
  breaks with weights; `extras` caps how many optional bits (fun fact, stats,
  ...) a break that long gets.
- **Pronunciations:** `pronunciations:` in `stations.yaml` fixes names the
  voices get wrong: Kokoro phonemes (`Sade: /ʃɑdˈA/`), a sound-alike spelling
  for any voice, or both. Find phonemes with the booth's "How does Kokoro say
  it?".
- **Swapping voices:** TTS providers are named in `stations.yaml`. Any
  OpenAI-compatible speech API works with `type: openai`; ElevenLabs is built in.
  Add the key to the `radio-dj` environment and point a DJ's `tts:` at it. New
  provider types are a small class in `app/tts.py`.
- DJ scripts come from Claude (`claude-haiku-5-5`) when `ANTHROPIC_API_KEY` is
  set. With no key, DJs read a plain "That was X by Y" template. If the key is
  rejected, they use the template and try Claude again an hour later. Any
  other API error falls back to the template for that one break. If TTS fails,
  the break is skipped. Either way the music keeps playing.

## Images

CI publishes two images on every push to `main` (`:main`, `:sha-xxxxxxx`) and
every `vX.Y.Z` tag (`:X.Y.Z`, `:X.Y`, `:latest`), for amd64 and arm64:

| Image | Built from | Notes |
|---|---|---|
| `ghcr.io/e13h/radio-dj` | `app/` | the DJ service on :8080; config at `/config/stations.yaml`, history in `/data`, audio in `/media` (shared with liquidsoap) |
| `ghcr.io/e13h/radio-dj/liquidsoap` | `liquidsoap/` | Liquidsoap with `radio.liq` baked in; env `DJ_URL`, `ICECAST_HOST`, `ICECAST_SOURCE_PASSWORD`, `STATIONS`, `STREAM_BITRATE` |

Use the same version tag for both. They also need Kokoro
(`ghcr.io/remsky/kokoro-fastapi-cpu`) and Icecast (`libretime/icecast`). The
deployment, including the compose file, secrets and the real `stations.yaml`,
is in [`e13h/selfhosted`](https://github.com/e13h/selfhosted/tree/main/services/xwing/radio).
`demo.sh` shows a minimal compose file for the whole stack.

To release: `git tag v0.2.0 && git push --tags`, then bump both image tags in
the selfhosted compose file.

## Setup

1. In Navidrome, create a regular (non-admin) user for the radio, e.g.
   `radio-bot`, with access to the libraries the stations play.
2. Deploy the four containers (see [Images](#images)) and set `NAVIDROME_URL`,
   `NAVIDROME_USER`, `NAVIDROME_PASSWORD` and optionally `ANTHROPIC_API_KEY`
   and `NAVIDROME_STATS_USER`/`NAVIDROME_STATS_PASSWORD` on `radio-dj`.
   After changing `stations.yaml`, restart `radio-dj`.
3. In Nautiline, add a radio station with the `.pls` URL and the homepage URL above.
   To also list the stations in Navidrome, add them under Radios in the web UI
   using the stream URL.
### Notes

- Navidrome play counts are per user. Without the stats user, the bot's (empty)
  counts are used, so rotation only knows what the radio itself has played and
  the stats segment has nothing to say.
- `scrobble: true` on a station records radio plays as plays for the stats user
  (or the bot). It's off by default so the radio doesn't inflate your stats.
- History lives in `/data/radio.db` (SQLite).
- The model is `claude-haiku-5-5` at `effort: low`: one short request per song.
  For livelier writing, set `llm.model: claude-sonnet-5-5` (and
  `fallbacks: true` if you want server-side refusal fallback).

## Local demo

Runs the whole stack on your laptop against the real Navidrome (over the LAN):

```bash
NAVIDROME_USER=radio-bot NAVIDROME_PASSWORD=... ANTHROPIC_API_KEY=... ./demo.sh up
./demo.sh logs    # watch the DJs
./demo.sh down    # stop (or `clean` to also delete history)
```

To make the DJ talk over the end of a song and the start of the next one (like
real radio), set `talk.overlap_seconds` (0–3) for a station in `stations.yaml`.
It defaults to 0: breaks play back to back with the music.

For quick debugging, `RADIO_PREVIEW_SECONDS=15 ./demo.sh up` plays only the
first 15 seconds of each song, so you hear a DJ break every ~20 seconds.

Open http://localhost:18080. The DJ booth is at http://localhost:18080/booth,
and Kokoro's own web UI (voices, blends, speeds) at http://localhost:18880/web. Generated files go to `~/.cache/radio-dj-demo`.
It uses `stations.example.yaml`; to try your real config, set
`STATIONS_FILE=../selfhosted/services/xwing/radio/config-radio/stations.yaml`
(Ansible variables like `{{ timezone }}` are filled in).

## Dry run: tune the scripts

`dryrun.sh` writes a dozen breaks for a station using the real library, play
counts, shuffle and Claude, without TTS, Docker or going on air. Nothing is
saved to the station history. Needs [uv](https://docs.astral.sh/uv/).

```bash
export NAVIDROME_USER=radio-bot NAVIDROME_PASSWORD=... ANTHROPIC_API_KEY=...
./dryrun.sh main                 # prints a seed
# edit talk:, personas or pronunciations in stations.example.yaml, then
./dryrun.sh main --seed 1234     # same songs, new scripts
```

Options: `-n 6` (number of breaks), `--deep-dive [ARTIST]`, `--sign-on`,
`--history` (start from the local demo's play history),
`--config path/to/stations.yaml`, `--help`.
On the server: `docker compose exec radio-dj python dryrun.py main --config /config/stations.yaml`.

## Tests

```bash
uv run --no-project --with-requirements app/requirements.txt --with pytest pytest tests
```
