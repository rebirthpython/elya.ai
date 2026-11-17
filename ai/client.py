"""AI-клиент: перебирает провайдеров и модели по приоритету (фолбэк),
временно отключает модели, упёршиеся в лимит, пропавшие из каталога или зависшие,
и соблюдает дневной лимит запросов."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from ai.providers.base import (
    BadResponseError,
    BaseProvider,
    ChatMessage,
    CompletionResult,
    ModelNotFoundError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnreachableError,
    RateLimitError,
)
from services.usage import UsageTracker

logger = logging.getLogger(__name__)

_END = object()  # поток модели закончился


@dataclass(eq=False)
class _Attempt:
    """Один запущенный потоковый запрос к модели."""

    provider: BaseProvider
    model: str
    started: float  # по часам клиента — для лога
    launched: float  # по часам event loop — для таймаутов
    events: AsyncIterator[str | CompletionResult]
    # Ждёт первое решающее событие потока: текст, CompletionResult, ошибку или _END.
    task: asyncio.Task | None = None


class AIError(Exception):
    """Базовая ошибка AI-клиента, которую обрабатывают хендлеры."""


class DailyLimitExceededError(AIError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"daily request limit reached ({limit})")
        self.limit = limit


class NoVisionModelError(AIError):
    """Ни одна подключённая модель не умеет работать с картинками."""

    def __init__(self) -> None:
        super().__init__("no vision-capable models configured")


class AllProvidersFailedError(AIError):
    def __init__(self, errors: list[ProviderError]) -> None:
        super().__init__("; ".join(str(e) for e in errors) or "all models are cooling down")
        self.errors = errors

    @property
    def kind(self) -> str:
        """rate_limit | config | unavailable — для понятного сообщения пользователю."""
        if not self.errors or any(isinstance(e, RateLimitError) for e in self.errors):
            return "rate_limit"
        if all(e.provider_level or isinstance(e, ModelNotFoundError) for e in self.errors):
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
        missing_model_cooldown: float = 6 * 3600.0,
        first_token_timeout: float = 0.0,
        hedge_delay: float = 0.0,
        max_parallel: int = 2,
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
        # Сколько ждать первый кусок ответа, прежде чем перейти к следующей модели.
        # 0 — без отдельного ограничения (действует только общий таймаут HTTP-запроса).
        self._first_token_timeout = first_token_timeout
        # Если модель молчит дольше hedge_delay, параллельно запускается следующая,
        # и отвечает та, что начнёт первой (0 — модели пробуются строго по очереди).
        self._hedge_delay = hedge_delay
        self._max_parallel = max(1, max_parallel)
        # Модель, которой нет в каталоге провайдера, проверяется заново раз в несколько часов.
        self._missing_model_cooldown = missing_model_cooldown
        self._clock = clock
        # (provider, model|None) -> (конец паузы, число ошибок подряд, последняя ошибка)
        self._cooldowns: dict[tuple[str, str | None], tuple[float, int, ProviderError]] = {}

    @property
    def provider_titles(self) -> list[str]:
        return [p.title for p in self._providers]

    @property
    def providers(self) -> list[BaseProvider]:
        return list(self._providers)

    def is_paused(self, provider: str, model: str | None = None) -> bool:
        """Модель (или весь провайдер при model=None) сейчас пропускается после ошибки."""
        if self._cooling_error((provider, None)) is not None:
            return True
        return model is not None and self._cooling_error((provider, model)) is not None

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()

    def _cooling_error(self, key: tuple[str, str | None]) -> ProviderError | None:
        """Ошибка, из-за которой модель/провайдер сейчас на паузе, иначе None."""
        entry = self._cooldowns.get(key)
        return entry[2] if entry is not None and entry[0] > self._clock() else None

    def _cool_down(
        self,
        key: tuple[str, str | None],
        error: ProviderError,
        delay: float | None = None,
        *,
        cap: float | None = None,
    ) -> None:
        strikes = self._cooldowns[key][1] + 1 if key in self._cooldowns else 1
        if delay is None:
            delay = self._base_cooldown * 2 ** (strikes - 1)
        limit = self._max_cooldown if cap is None else cap
        self._cooldowns[key] = (self._clock() + min(delay, limit), strikes, error)

    @property
    def available_models(self) -> list[tuple[BaseProvider, str]]:
        """Все пары (провайдер, модель) в порядке приоритета."""
        return [(provider, model) for provider in self._providers for model in provider.models]

    @property
    def vision_models(self) -> list[tuple[BaseProvider, str]]:
        """Пары (провайдер, модель), которые понимают картинки."""
        return [(p, model) for p in self._providers for model in p.vision_models]

    @property
    def has_vision(self) -> bool:
        return bool(self.vision_models)

    def _candidates(
        self, prefer: tuple[str, str] | None, vision: bool = False
    ) -> list[tuple[BaseProvider, str]]:
        candidates = self.vision_models if vision else self.available_models
        if prefer is not None:
            # Стабильная сортировка: выбранная модель первой, остальные — в прежнем порядке.
            candidates.sort(key=lambda pair: (pair[0].name, pair[1]) != prefer)
        return candidates

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        prefer: tuple[str, str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """Возвращает ответ первой сработавшей модели.

        prefer=(провайдер, модель) — с какой модели начать; при её отказе
        остальные пробуются в обычном порядке. temperature и max_tokens
        переопределяют значения по умолчанию для служебных запросов.
        """
        vision = any(m.images for m in messages)
        if vision and not self.has_vision:
            raise NoVisionModelError
        errors: list[ProviderError] = []
        for provider, model in self._candidates(prefer, vision):
            if self._skip_paused(provider, model, errors):
                continue
            await self._consume()
            started = self._clock()
            try:
                result = await provider.complete(
                    messages, **self._options(provider, model, temperature, max_tokens)
                )
            except ProviderError as exc:
                self._record_failure(provider, model, exc, errors)
                continue
            self._record_success(provider, model, result, started, "")
            return result
        raise AllProvidersFailedError(errors)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        prefer: tuple[str, str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str | CompletionResult]:
        """Ответ по частям: строки — новый текст по мере генерации, последним — CompletionResult.

        Пока модель не начала отвечать, при ошибке пробуется следующая; если она молчит
        дольше hedge_delay, следующая запускается параллельно, и ответ даёт та, что начнёт
        первой (остальные отменяются). Если поток оборвался на середине, бросается
        AllProvidersFailedError: часть ответа уже показана, и незаметно подменить модель нельзя.
        """
        vision = any(m.images for m in messages)
        if vision and not self.has_vision:
            raise NoVisionModelError
        loop = asyncio.get_running_loop()
        candidates = iter(self._candidates(prefer, vision))
        errors: list[ProviderError] = []
        running: list[_Attempt] = []  # ещё не начали отвечать
        launched: list[_Attempt] = []  # все запущенные — чтобы закрыть в конце
        exhausted = False
        last_launch = 0.0
        winner: _Attempt | None = None
        item: object = None

        async def launch() -> bool:
            nonlocal exhausted, last_launch
            for provider, model in candidates:
                if self._skip_paused(provider, model, errors):
                    continue
                await self._consume()
                options = self._options(provider, model, temperature, max_tokens)
                attempt = _Attempt(provider, model, self._clock(), loop.time(), provider.stream(messages, **options))
                attempt.task = asyncio.create_task(self._first_event(attempt.events))
                running.append(attempt)
                launched.append(attempt)
                last_launch = attempt.launched
                return True
            exhausted = True
            return False

        try:
            while winner is None:
                hedging = (
                    self._hedge_delay > 0 and not exhausted and 0 < len(running) < self._max_parallel
                )
                if not running or (hedging and loop.time() - last_launch >= self._hedge_delay):
                    if not await launch() and not running:
                        raise AllProvidersFailedError(errors)
                    continue

                deadlines = [last_launch + self._hedge_delay] if hedging else []
                if self._first_token_timeout:
                    deadlines += [a.launched + self._first_token_timeout for a in running]
                timeout = max(0.0, min(deadlines) - loop.time()) if deadlines else None
                await asyncio.wait([a.task for a in running], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

                now = loop.time()
                for attempt in list(running):  # по порядку запуска: при равенстве — приоритетная модель
                    if attempt.task.done():
                        item = attempt.task.result()
                        error = self._as_error(attempt, item)
                        if error is None:
                            winner = attempt
                            break
                        running.remove(attempt)
                        self._record_failure(attempt.provider, attempt.model, error, errors)
                    elif self._first_token_timeout and now - attempt.launched >= self._first_token_timeout:
                        # Зависшая модель: дальше её не ждём.
                        attempt.task.cancel()
                        running.remove(attempt)
                        error = ProviderTimeoutError(
                            f"no first token within {self._first_token_timeout:g}s",
                            provider=attempt.provider.name,
                            model=attempt.model,
                        )
                        self._record_failure(attempt.provider, attempt.model, error, errors)

            # Победитель начал отвечать — остальные больше не нужны.
            for attempt in running:
                if attempt is not winner:
                    attempt.task.cancel()
            while True:
                if isinstance(item, CompletionResult):
                    self._record_success(winner.provider, winner.model, item, winner.started, " (stream)")
                    yield item
                    return
                error = self._as_error(winner, item)
                if error is not None:
                    self._record_failure(winner.provider, winner.model, error, errors)
                    raise AllProvidersFailedError([error]) from error
                yield item
                # Дальше поток победителя читается напрямую: модель не генерирует впрок,
                # и если ответ перестали читать, она останавливается сразу.
                item = await self._first_event(winner.events)
        finally:
            # Ответ дочитан или его перестали читать: запросы и HTTP-соединения закрываются сразу.
            for attempt in launched:
                attempt.task.cancel()
            await asyncio.gather(*(a.task for a in launched), return_exceptions=True)
            for attempt in launched:
                close = getattr(attempt.events, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 — закрытие не должно ломать ответ
                        logger.debug("Closing a model stream failed", exc_info=True)

    @staticmethod
    async def _first_event(events: AsyncIterator[str | CompletionResult]) -> object:
        """Следующее значимое событие потока: текст, CompletionResult, ошибка или _END."""
        try:
            async for event in events:  # выход из цикла не закрывает поток
                if isinstance(event, CompletionResult) or event:
                    return event
            return _END
        except Exception as exc:  # noqa: BLE001 — разбирается в stream()
            return exc

    @staticmethod
    def _as_error(attempt: _Attempt, item: object) -> ProviderError | None:
        """None — это часть ответа; иначе ошибка попытки (не ProviderError пробрасывается)."""
        if item is _END:
            return BadResponseError(
                "stream ended without a result", provider=attempt.provider.name, model=attempt.model
            )
        if isinstance(item, ProviderError):
            return item
        if isinstance(item, BaseException):
            raise item
        return None

    def _skip_paused(self, provider: BaseProvider, model: str, errors: list[ProviderError]) -> bool:
        paused = self._cooling_error((provider.name, None)) or self._cooling_error((provider.name, model))
        if paused is None:
            return False
        # Пауза провайдера даёт одну и ту же ошибку для всех его моделей.
        if not any(paused is seen for seen in errors):
            errors.append(paused)
        return True

    async def _consume(self) -> None:
        if self._usage and not await self._usage.try_consume():
            raise DailyLimitExceededError(self._usage.daily_limit)

    def _options(
        self, provider: BaseProvider, model: str, temperature: float | None, max_tokens: int | None
    ) -> dict:
        limit = max_tokens if max_tokens is not None else self._max_tokens
        if provider.max_output_tokens:
            limit = min(limit, provider.max_output_tokens)
        return {
            "model": model,
            "max_tokens": limit,
            "temperature": temperature if temperature is not None else self._temperature,
        }

    def _record_failure(
        self, provider: BaseProvider, model: str, exc: ProviderError, errors: list[ProviderError]
    ) -> None:
        logger.warning("AI request failed: %s", exc)
        errors.append(exc)
        key = (provider.name, model)
        if exc.provider_level:
            # Остальные модели этого провайдера будут пропущены как «на паузе».
            self._cool_down((provider.name, None), exc, self._base_cooldown * 10)
        elif isinstance(exc, RateLimitError):
            self._cool_down(key, exc, exc.retry_after)
        elif isinstance(exc, ModelNotFoundError):
            # Модель убрали из бесплатного каталога: не тратить на неё каждый запрос.
            delay = self._missing_model_cooldown
            self._cool_down(key, exc, delay, cap=delay)
        elif isinstance(exc, ProviderUnreachableError):
            # Сервер недоступен целиком (сеть, DNS, блокировка): его модели не ждём.
            self._cool_down((provider.name, None), exc)
        elif isinstance(exc, ProviderTimeoutError):
            # Зависшая модель: следующие запросы не ждут её таймаута.
            self._cool_down(key, exc)

    def _record_success(
        self, provider: BaseProvider, model: str, result: CompletionResult, started: float, note: str
    ) -> None:
        self._cooldowns.pop((provider.name, model), None)
        self._cooldowns.pop((provider.name, None), None)
        logger.info(
            "AI response: provider=%s model=%s chars=%d time=%.1fs%s",
            result.provider,
            result.model,
            len(result.text),
            self._clock() - started,
            note,
        )
