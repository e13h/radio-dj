#!/usr/bin/env bash
# Write a dozen DJ breaks for a station, without going on air. Runs on this
# computer against the real Navidrome; needs uv (https://docs.astral.sh/uv/).
#
#   NAVIDROME_USER=radio-bot NAVIDROME_PASSWORD=... ANTHROPIC_API_KEY=... ./dryrun.sh main
#   ./dryrun.sh main --seed 1234      # same songs as an earlier run
#   ./dryrun.sh kids -n 6 --deep-dive # start with an artist deep dive
#   ./dryrun.sh --help
#
# Reads stations.example.yaml by default; --config path/to/stations.yaml for another.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec uv run --quiet --no-project --python 3.12 \
  --with-requirements "$HERE/app/requirements.txt" \
  python "$HERE/app/dryrun.py" "$@"
