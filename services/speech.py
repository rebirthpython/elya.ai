"""Распознавание голосовых сообщений через Groq Whisper (бесплатный тариф, ключ GROQ_API_KEY)."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Без явного языка Whisper угадывает его по первым секундам записи и на коротких или тихих
# голосовых часто ошибается: решает, что это английский, и выдаёт случайные английские слова.
DEFAULT_LANGUAGE = "ru"

# Отсев «додуманных» фраз по правилу самого Whisper: если модель считает, что речи в отрезке
# скорее нет, и при этом не уверена в тексте, — это шум или тишина, а не слова.
NO_SPEECH_PROB = 0.6
MIN_AVG_LOGPROB = -1.0


class SpeechError(Exception):
    pass


class SpeechToText:
    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        *,
        model: str,
        language: str = DEFAULT_LANGUAGE,
        url: str = GROQ_STT_URL,
    ) -> None:
        self._key = api_key
        self._http = http_client
        self._model = model
        self._language = language  # пустая строка — язык определяет сам Whisper
        self._url = url

    async def transcribe(
        self, audio: bytes, filename: str = "voice.ogg", content_type: str = "audio/ogg"
    ) -> str:
        data = {"model": self._model, "response_format": "verbose_json"}
        if self._language:
            data["language"] = self._language
        try:
            response = await self._http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (filename, audio, content_type)},
                data=data,
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"network error: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SpeechError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SpeechError("bad response") from exc
        if not isinstance(payload, dict):
            raise SpeechError("bad response")
        return _speech_text(payload)


def _speech_text(payload: dict) -> str:
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        parts = [
            str(segment.get("text", ""))
            for segment in segments
            if isinstance(segment, dict) and not _is_silence(segment)
        ]
        text = " ".join(parts)
    else:
        text = str(payload.get("text") or "")
    return " ".join(text.split())


def _is_silence(segment: dict) -> bool:
    try:
        no_speech = float(segment.get("no_speech_prob", 0.0))
        logprob = float(segment.get("avg_logprob", 0.0))
    except (TypeError, ValueError):
        return False
    return no_speech > NO_SPEECH_PROB and logprob < MIN_AVG_LOGPROB
