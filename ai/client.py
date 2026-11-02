"""AI-клиент: перебирает провайдеров и модели по приоритету (фолбэк),
временно отключает модели, упёршиеся в лимит, и соблюдает дневной лимит запросов."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ai.providers.base import (
    BaseProvider,
    ChatMessage,
    CompletionResult,
    ProviderError,
    RateLimitError,
)
from services.usage import UsageTracker

logger = logging.getLogger(__name__)


class AIError(Exception):
    """Базовая ошибка AI-клиента, которую обрабатывают хендлеры."""


class DailyLimitExceededError(AIError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"daily request limit reached ({limit})")
        self.limit = limit


class AllProvidersFailedError(AIError):
    def __init__(self, errors: list[ProviderError]) -> None:
        super().__init__("; ".join(str(e) for e in errors) or "all models are cooling down")
        self.errors = errors

    @property
    def kind(self) -> str:
        """rate_limit | config | unavailable — для понятного сообщения пользователю."""
        if not self.errors or any(isinstance(e, RateLimitError) for e in self.errors):
            return "rate_limit"
        if all(e.provider_level for e in self.errors):
            return "config"
        return "unavailable"


class AIClient:
    def __init__(
        self,
        providers: list[BaseProvider],
        *,
        max_tokens: int,
        temperature: float,
        usage: UsageTracker | None = None,
        base_cooldown: float = 60.0,
        max_cooldown: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = providers
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._usage = usage
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._clock = clock
        # (provider, model|None) -> (конец паузы, число ошибок подряд, последняя ошибка)
        self._cooldowns: dict[tuple[str, str | None], tuple[float, int, ProviderError]] = {}

    @property
    def provider_titles(self) -> list[str]:
        return [p.title for p in self._providers]

    def _cooling_error(self, key: tuple[str, str | None]) -> ProviderError | None:
        """Ошибка, из-за которой модель/провайдер сейчас на паузе, иначе None."""
        entry = self._cooldowns.get(key)
        return entry[2] if entry is not None and entry[0] > self._clock() else None

    def _cool_down(
        self, key: tuple[str, str | None], error: ProviderError, delay: float | None = None
    ) -> None:
        strikes = self._cooldowns[key][1] + 1 if key in self._cooldowns else 1
        if delay is None:
            delay = self._base_cooldown * 2 ** (strikes - 1)
        self._cooldowns[key] = (self._clock() + min(delay, self._max_cooldown), strikes, error)

    async def complete(self, messages: list[ChatMessage]) -> CompletionResult:
        errors: list[ProviderError] = []

        for provider in self._providers:
            if paused := self._cooling_error((provider.name, None)):
                errors.append(paused)
                continue
            for model in provider.models:
                key = (provider.name, model)
                if paused := self._cooling_error(key):
                    errors.append(paused)
                    continue
                if self._usage and not await self._usage.try_consume():
                    raise DailyLimitExceededError(self._usage.daily_limit)

                started = self._clock()
                try:
                    result = await provider.complete(
                        messages,
                        model=model,
                        max_tokens=self._max_tokens,
                        temperature=self._temperature,
                    )
                except ProviderError as exc:
                    logger.warning("AI request failed: %s", exc)
                    errors.append(exc)
                    if exc.provider_level:
                        self._cool_down((provider.name, None), exc, self._base_cooldown * 10)
                        break
                    if isinstance(exc, RateLimitError):
                        self._cool_down(key, exc, exc.retry_after)
                    continue

                self._cooldowns.pop(key, None)
                self._cooldowns.pop((provider.name, None), None)
                logger.info(
                    "AI response: provider=%s model=%s chars=%d time=%.1fs",
                    result.provider,
                    result.model,
                    len(result.text),
                    self._clock() - started,
                )
                return result

        raise AllProvidersFailedError(errors)
