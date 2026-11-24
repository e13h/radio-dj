"""How many people are tuned in to each station, from Icecast's public status page."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

CACHE_SECONDS = 5.0


class Listeners:
    def __init__(self, status_url: str):
        self.status_url = status_url
        self._counts: dict[str, int] | None = None
        self._fetched_at = 0.0

    async def count(self, station_id: str) -> int:
        """Listeners on <station_id>.mp3. 0 if Icecast can't be reached or the mount is missing."""
        if time.monotonic() - self._fetched_at > CACHE_SECONDS:
            self._counts = await self._fetch()
            self._fetched_at = time.monotonic()
        return (self._counts or {}).get(station_id, 0)

    async def _fetch(self) -> dict[str, int] | None:
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                r = await http.get(self.status_url)
                r.raise_for_status()
                return parse_status(r.json())
        except Exception as e:
            log.warning("Icecast status unavailable (%s); treating stations as having no listeners", e)
            return None


def parse_status(status: dict) -> dict[str, int]:
    """station id -> listeners. Icecast gives one source as an object, several as a list."""
    sources = status.get("icestats", {}).get("source") or []
    if isinstance(sources, dict):
        sources = [sources]
    counts = {}
    for s in sources:
        mount = str(s.get("listenurl", "")).rsplit("/", 1)[-1]
        if mount.endswith(".mp3"):
            counts[mount.removesuffix(".mp3")] = int(s.get("listeners") or 0)
    return counts
