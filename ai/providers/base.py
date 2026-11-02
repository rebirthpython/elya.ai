"""Общие типы и ошибки AI-провайдеров."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class CompletionResult:
    text: str
    provider: str
    model: str
    truncated: bool = False


class ProviderError(Exception):
    """Базовая ошибка провайдера.

    provider_level=True означает, что проблема касается всего провайдера
    (ключ, регион, баланс) и остальные его модели пробовать бессмысленно.
    """

    provider_level: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status_code = status_code

    def __str__(self) -> str:
        where = f"{self.provider}/{self.model}" if self.model else self.provider
        code = f" HTTP {self.status_code}" if self.status_code else ""
        return f"[{where}{code}] {self.args[0]}"


class RateLimitError(ProviderError):
    """429: превышен лимит запросов/квота модели."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model, status_code=status_code)
        self.retry_after = retry_after


class AuthError(ProviderError):
    """Неверный или отсутствующий API-ключ, нет доступа."""

    provider_level = True


class RegionNotSupportedError(ProviderError):
    """Провайдер недоступен из страны, где запущен бот."""

    provider_level = True


class InsufficientCreditsError(ProviderError):
    """402: недостаточно средств/кредитов у провайдера."""

    provider_level = True


class ModelNotFoundError(ProviderError):
    """Модель не существует или снята с поддержки."""


class ProviderUnavailableError(ProviderError):
    """Таймаут, сетевая ошибка, 5xx."""


class BadResponseError(ProviderError):
    """Провайдер вернул некорректный, пустой или заблокированный ответ."""


class BaseProvider(ABC):
    """Интерфейс AI-провайдера. Новые провайдеры реализуют complete()."""

    def __init__(self, name: str, title: str, models: tuple[str, ...]) -> None:
        self.name = name
        self.title = title
        self.models = models

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        raise NotImplementedError
