"""Распознавание голосовых сообщений через Groq Whisper (бесплатный тариф, ключ GROQ_API_KEY)."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class SpeechError(Exception):
    pass


class SpeechToText:
    def __init__(self, api_key: str, http_client: httpx.AsyncClient, *, model: str, url: str = GROQ_STT_URL) -> None:
        self._key = api_key
        self._http = http_client
        self._model = model
        self._url = url

    async def transcribe(
        self, audio: bytes, filename: str = "voice.ogg", content_type: str = "audio/ogg"
    ) -> str:
        try:
            response = await self._http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (filename, audio, content_type)},
                data={"model": self._model, "response_format": "json"},
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"network error: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SpeechError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            text = response.json().get("text", "")
        except (ValueError, AttributeError) as exc:
            raise SpeechError("bad response") from exc
        return " ".join(str(text).split())
