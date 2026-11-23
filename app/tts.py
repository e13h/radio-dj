"""Text-to-speech providers.

Every provider implements one method: `synthesize(text, voice, dest, speed)`
that writes an MP3 to dest. To add a provider, write a class with that method and
register it in PROVIDER_TYPES; stations.yaml then selects it by name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import httpx

from config import TTSProviderConfig


class TTSProvider(Protocol):
    async def synthesize(self, text: str, voice: str, dest: Path, speed: float | None = None) -> None: ...


def _api_key(cfg: TTSProviderConfig) -> str | None:
    if not cfg.api_key_env:
        return None
    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise RuntimeError(f"environment variable {cfg.api_key_env} is not set")
    return key


async def _write(response: httpx.Response, dest: Path) -> None:
    response.raise_for_status()
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(response.content)
    tmp.rename(dest)


class OpenAICompatibleTTS:
    """POST /audio/speech: Kokoro-FastAPI (local), OpenAI, and other compatible servers."""

    def __init__(self, cfg: TTSProviderConfig):
        if not cfg.base_url:
            raise ValueError("openai-type TTS provider needs base_url")
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=120.0)

    async def synthesize(self, text: str, voice: str, dest: Path, speed: float | None = None) -> None:
        headers = {}
        if key := _api_key(self.cfg):
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": self.cfg.model or "tts-1", "input": text, "voice": voice, "response_format": "mp3"}
        if speed := speed or self.cfg.speed:
            body["speed"] = speed
        r = await self.http.post(f"{self.cfg.base_url.rstrip('/')}/audio/speech", json=body, headers=headers)
        await _write(r, dest)


class ElevenLabsTTS:
    def __init__(self, cfg: TTSProviderConfig):
        self.cfg = cfg
        self.base_url = (cfg.base_url or "https://api.elevenlabs.io/v1").rstrip("/")
        self.http = httpx.AsyncClient(timeout=120.0)

    async def synthesize(self, text: str, voice: str, dest: Path, speed: float | None = None) -> None:
        body: dict = {"text": text, "model_id": self.cfg.model or "eleven_multilingual_v2"}
        if speed := speed or self.cfg.speed:
            body["voice_settings"] = {"speed": speed}
        r = await self.http.post(
            f"{self.base_url}/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": _api_key(self.cfg) or ""},
            json=body,
        )
        await _write(r, dest)


PROVIDER_TYPES = {
    "openai": OpenAICompatibleTTS,
    "elevenlabs": ElevenLabsTTS,
}


def build_providers(providers: dict[str, TTSProviderConfig]) -> dict[str, TTSProvider]:
    return {name: PROVIDER_TYPES[cfg.type](cfg) for name, cfg in providers.items()}
