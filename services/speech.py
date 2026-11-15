"""Распознавание речи через Groq Whisper (бесплатный тариф, ключ GROQ_API_KEY).

transcribe — текст голосового сообщения; transcribe_timed — текст со временем фраз и слов
(для субтитров и монтажа видео по смыслу). Оба способа передают Whisper язык и отсеивают
то, что он «услышал» в тишине.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

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

# На тишине и музыке Whisper иногда «слышит» титры из обучающих данных.
_HALLUCINATIONS = re.compile(
    r"dimatorzok|субтитр\w*\s+(?:сделал|создавал|делал|подготовил)|редактор субтитров|"
    r"корректор\s+а\.|продолжение следует",
    re.IGNORECASE,
)


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


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_silence(segment: dict) -> bool:
    try:
        no_speech = float(segment.get("no_speech_prob", 0.0))
        logprob = float(segment.get("avg_logprob", 0.0))
    except (TypeError, ValueError):
        return False
    return no_speech > NO_SPEECH_PROB and logprob < MIN_AVG_LOGPROB


def _is_noise(segment: dict) -> bool:
    """Фраза, которой на самом деле не было: тишина по оценке Whisper или «титры»."""
    return _is_silence(segment) or bool(_HALLUCINATIONS.search(str(segment.get("text", ""))))


def _speech_text(payload: dict) -> str:
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        parts = [
            str(segment.get("text", ""))
            for segment in segments
            if isinstance(segment, dict) and not _is_noise(segment)
        ]
        text = " ".join(parts)
    else:
        text = str(payload.get("text") or "")
    return " ".join(text.split())


def parse_verbose(data: dict[str, Any]) -> TimedTranscript:
    """Ответ verbose_json -> фразы и слова; фразы, «услышанные» в тишине, выбрасываются."""
    segments: list[Timed] = []
    dropped: list[tuple[float, float]] = []
    for raw in data.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _float(raw.get("start")), _float(raw.get("end"))
        text = " ".join(str(raw.get("text", "")).split())
        if not text or end <= start:
            continue
        if _is_noise(raw):
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

    async def _request(self, audio: bytes, filename: str, content_type: str, data: dict[str, Any],
                       timeout: float | None = None) -> dict[str, Any]:
        fields: dict[str, Any] = {"model": self._model, "response_format": "verbose_json", **data}
        if self._language:
            fields["language"] = self._language
        try:
            response = await self._http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (filename, audio, content_type)},
                data=fields,
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
        return _speech_text(await self._request(audio, filename, content_type, {}))

    async def transcribe_timed(
        self, audio: bytes, filename: str = "audio.m4a", content_type: str = "audio/mp4"
    ) -> TimedTranscript:
        payload = await self._request(
            audio, filename, content_type,
            {"timestamp_granularities[]": ["word", "segment"]},
            timeout=180.0,
        )
        return parse_verbose(payload)
