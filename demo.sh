#!/usr/bin/env bash
# Run the AI radio on this computer against your real Navidrome.
#
#   NAVIDROME_USER=radio-bot NAVIDROME_PASSWORD=... ./demo.sh up
#   ./demo.sh logs     # follow what the DJs are saying
#   ./demo.sh down     # stop and remove containers (keeps play history)
#   ./demo.sh clean    # stop and delete all demo files
#
# Optional env:
#   ANTHROPIC_API_KEY            Claude-written breaks (template breaks without it)
#   NAVIDROME_URL                default http://xwing.local:4533
#   NAVIDROME_STATS_USER/_PASSWORD  read play counts as you instead of the bot
#   RADIO_PREVIEW_SECONDS        debug: play only N seconds of each song (e.g. 15)
#   STATIONS_FILE                stations.yaml to use (default: stations.example.yaml);
#                                Ansible vars like {{ timezone }} are filled in
#
# Then open http://localhost:18080 (homepage + players) or point any player
# at http://localhost:18000/main.mp3 / kids.mp3. The DJ booth is at
# http://localhost:18080/booth and Kokoro's own web UI at http://localhost:18880/web.
#
# Secrets are passed through from your shell environment and never written
# to disk.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEMO="${XDG_CACHE_HOME:-$HOME/.cache}/radio-dj-demo"
STATIONS_FILE="${STATIONS_FILE:-$HERE/stations.example.yaml}"
export COMPOSE_PROJECT_NAME=radio-demo
compose() { docker compose -f "$DEMO/compose.yaml" "$@"; }

render() {
  : "${NAVIDROME_USER:?set NAVIDROME_USER (the Navidrome account the radio uses)}"
  : "${NAVIDROME_PASSWORD:?set NAVIDROME_PASSWORD}"
  export NAVIDROME_URL="${NAVIDROME_URL:-http://xwing.local:4533}"
  local tz
  tz="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"

  mkdir -p "$DEMO/data" "$DEMO/media"
  # Fill in the Ansible variables the way the server deploy would.
  sed -e "s#{{ timezone }}#$tz#" \
      -e 's#https://radio\.{{ vault_tailscale_magicdns_domain }}#http://localhost:18080#' \
      -e 's#https://radio-stream\.{{ vault_tailscale_magicdns_domain }}#http://localhost:18000#' \
      "$STATIONS_FILE" > "$DEMO/stations.yaml"
  if grep -q '{{' "$DEMO/stations.yaml"; then
    echo "stations.yaml has Ansible variables this script doesn't know:" >&2
    grep -n '{{' "$DEMO/stations.yaml" >&2
    exit 1
  fi
  local stations
  stations="$(python3 -c 'import re,sys; t=open(sys.argv[1]).read().split("\nstations:",1)[1]; print(",".join(re.findall(r"^  (\w+):", t, re.M)))' "$DEMO/stations.yaml")"

  cat > "$DEMO/compose.yaml" <<EOF
services:
  radio-dj:
    build: $HERE/app
    image: radio-dj:local
    environment:
      TZ: $tz
      NAVIDROME_URL: \${NAVIDROME_URL}
      NAVIDROME_USER: \${NAVIDROME_USER}
      NAVIDROME_PASSWORD: \${NAVIDROME_PASSWORD}
      NAVIDROME_STATS_USER: \${NAVIDROME_STATS_USER:-}
      NAVIDROME_STATS_PASSWORD: \${NAVIDROME_STATS_PASSWORD:-}
      ANTHROPIC_API_KEY: \${ANTHROPIC_API_KEY:-}
      RADIO_PREVIEW_SECONDS: \${RADIO_PREVIEW_SECONDS:-}
    volumes:
      - $DEMO/stations.yaml:/config/stations.yaml:ro
      - $DEMO/data:/data
      - $DEMO/media:/media
    ports: ["18080:8080"]
    depends_on:
      radio-kokoro: { condition: service_healthy }
  radio-kokoro:
    image: ghcr.io/remsky/kokoro-fastapi-cpu:latest
    ports: ["18880:8880"]
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://127.0.0.1:8880/health"]
      interval: 5s
      start_period: 120s
  radio-liquidsoap:
    build: $HERE/liquidsoap
    image: radio-dj-liquidsoap:local
    environment:
      TZ: $tz
      DJ_URL: http://radio-dj:8080
      ICECAST_HOST: radio-icecast
      ICECAST_SOURCE_PASSWORD: demo-source
      STATIONS: "$stations"
    volumes:
      - $DEMO/media:/media:ro
    depends_on:
      radio-dj: { condition: service_healthy }
      radio-icecast: { condition: service_started }
  radio-icecast:
    image: libretime/icecast:2.4.4
    environment:
      ICECAST_SOURCE_PASSWORD: demo-source
      ICECAST_ADMIN_PASSWORD: demo-admin
    ports: ["18000:8000"]
EOF
}

case "${1:-up}" in
  up)
    render
    compose up -d --build
    echo
    echo "Homepage:  http://localhost:18080"
    echo "Streams:   http://localhost:18000/<station>.mp3"
    echo "DJ booth:  http://localhost:18080/booth"
    echo "Kokoro UI: http://localhost:18880/web"
    echo "DJ log:    $0 logs"
    [ -n "${ANTHROPIC_API_KEY:-}" ] || echo "(no ANTHROPIC_API_KEY: DJs will read template breaks)"
    [ -z "${RADIO_PREVIEW_SECONDS:-}" ] || echo "(preview mode: songs cut to ${RADIO_PREVIEW_SECONDS}s)"
    ;;
  render) render; echo "Wrote $DEMO/compose.yaml and $DEMO/stations.yaml" ;;
  logs) compose logs -f radio-dj ;;
  down) compose down ;;
  clean) compose down -v 2>/dev/null || true; rm -rf "$DEMO" ;;
  *) echo "usage: $0 [up|render|logs|down|clean]" >&2; exit 2 ;;
esac
