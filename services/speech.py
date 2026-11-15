"""Распознавание речи через Groq Whisper (бесплатный тариф, ключ GROQ_API_KEY).

transcribe — текст голосового сообщения; transcribe_timed — текст со временем фраз и слов
(для субтитров и монтажа по смыслу).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Whisper на тишине и музыке иногда «слышит» титры из обучающих данных.
_HALLUCINATIONS = re.compile(
    r"dimatorzok|субтитр\w*\s+(?:сделал|создавал|делал|подготовил)|редактор субтитров|"
    r"корректор\s+а\.|продолжение следует",
    re.IGNORECASE,
)
_NO_SPEECH = 0.6


class SpeechError(Exception):
    pass


@dataclass(frozen=True)
class Timed:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TimedTranscript:
    text: str
    segments: list[Timed] = field(default_factory=list)
    words: list[Timed] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TimedTranscript:
        return cls(
            text=str(data.get("text", "")),
            segments=[Timed(**s) for s in data.get("segments", [])],
            words=[Timed(**w) for w in data.get("words", [])],
        )


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_verbose(data: dict[str, Any]) -> TimedTranscript:
    """Ответ verbose_json -> фразы и слова; фразы-«галлюцинации» на тишине выбрасываются."""
    segments: list[Timed] = []
    dropped: list[tuple[float, float]] = []
    for raw in data.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _float(raw.get("start")), _float(raw.get("end"))
        text = " ".join(str(raw.get("text", "")).split())
        if not text or end <= start:
            continue
        if _float(raw.get("no_speech_prob")) > _NO_SPEECH or _HALLUCINATIONS.search(text):
            dropped.append((start, end))
            continue
        segments.append(Timed(round(start, 2), round(end, 2), text))

    words: list[Timed] = []
    for raw in data.get("words") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _float(raw.get("start")), _float(raw.get("end"))
        text = " ".join(str(raw.get("word", "")).split())
        middle = (start + end) / 2
        if text and end >= start and not any(a <= middle <= b for a, b in dropped):
            words.append(Timed(round(start, 2), round(end, 2), text))
    text = " ".join(s.text for s in segments) if segments or dropped else " ".join(str(data.get("text", "")).split())
    return TimedTranscript(text=text, segments=segments, words=words)


class SpeechToText:
    def __init__(self, api_key: str, http_client: httpx.AsyncClient, *, model: str, url: str = GROQ_STT_URL) -> None:
        self._key = api_key
        self._http = http_client
        self._model = model
        self._url = url

    async def _request(self, audio: bytes, filename: str, content_type: str, data: dict[str, Any],
                       timeout: float | None = None) -> dict[str, Any]:
        try:
            response = await self._http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (filename, audio, content_type)},
                data={"model": self._model, **data},
                **({"timeout": timeout} if timeout else {}),
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
        return payload

    async def transcribe(
        self, audio: bytes, filename: str = "voice.ogg", content_type: str = "audio/ogg"
    ) -> str:
        payload = await self._request(audio, filename, content_type, {"response_format": "json"})
        return " ".join(str(payload.get("text", "")).split())

    async def transcribe_timed(
        self, audio: bytes, filename: str = "audio.m4a", content_type: str = "audio/mp4"
    ) -> TimedTranscript:
        payload = await self._request(
            audio, filename, content_type,
            {"response_format": "verbose_json", "timestamp_granularities[]": ["word", "segment"]},
            timeout=180.0,
        )
        return parse_verbose(payload)
