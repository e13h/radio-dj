"""Minimal async Subsonic API client for Navidrome."""

from __future__ import annotations

import hashlib
import html
import logging
import re
import secrets
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class SubsonicError(RuntimeError):
    pass


class Subsonic:
    def __init__(self, base_url: str, user: str, password: str, client_name: str = "ai-radio"):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self._password = password
        self.client_name = client_name
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0))

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()
        return {"u": self.user, "t": token, "s": salt, "v": "1.16.1", "c": self.client_name, "f": "json"}

    async def call(self, endpoint: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        r = await self._http.get(f"{self.base_url}/rest/{endpoint}", params=self._auth() | params)
        r.raise_for_status()
        body = r.json()["subsonic-response"]
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise SubsonicError(f"{endpoint}: {err.get('code')} {err.get('message')}")
        return body

    async def ping(self) -> None:
        await self.call("ping")

    async def music_folders(self) -> list[dict]:
        body = await self.call("getMusicFolders")
        return body.get("musicFolders", {}).get("musicFolder", [])

    async def all_songs(self, folder_id: str | int, page_size: int = 500) -> list[dict]:
        """Every song in one library. Navidrome returns everything for query '""'."""
        songs: list[dict] = []
        offset = 0
        while True:
            body = await self.call(
                "search3",
                query='""',
                artistCount=0,
                albumCount=0,
                songCount=page_size,
                songOffset=offset,
                musicFolderId=folder_id,
            )
            page = body.get("searchResult3", {}).get("song", [])
            songs.extend(page)
            if len(page) < page_size:
                return songs
            offset += page_size

    async def random_songs(self, folder_id: str | int, size: int = 50) -> list[dict]:
        body = await self.call("getRandomSongs", size=size, musicFolderId=folder_id)
        return body.get("randomSongs", {}).get("song", [])

    async def artist_bio(self, artist_id: str) -> str:
        """Artist biography from Navidrome's metadata agents (Last.fm etc.), as plain text."""
        try:
            body = await self.call("getArtistInfo2", id=artist_id, count=0)
        except (SubsonicError, httpx.HTTPError) as e:
            log.info("No artist info for %s: %s", artist_id, e)
            return ""
        bio = body.get("artistInfo2", {}).get("biography", "") or ""
        bio = html.unescape(re.sub(r"<[^>]+>", "", bio))
        # Last.fm bios end with a "Read more on Last.fm" link.
        bio = re.sub(r"\s*Read more on Last\.fm.*$", "", bio, flags=re.S | re.I)
        return bio.strip()

    async def download(self, song_id: str, dest: Path) -> None:
        """Fetch the original file (no transcoding) to dest."""
        params = self._auth() | {"id": song_id, "format": "raw"}
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with self._http.stream("GET", f"{self.base_url}/rest/stream", params=params) as r:
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith(("application/json", "text/xml")):
                raise SubsonicError(f"stream {song_id}: {(await r.aread())[:200]!r}")
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    f.write(chunk)
        tmp.rename(dest)

    async def scrobble(self, song_id: str, played_at: float | None = None) -> None:
        await self.call(
            "scrobble", id=song_id, submission="true", time=int((played_at or time.time()) * 1000)
        )

    async def aclose(self) -> None:
        await self._http.aclose()
