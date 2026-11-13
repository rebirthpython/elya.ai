"""Точка входа: сборка зависимостей, запуск бота (long polling) и сервера Mini App."""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from ai.client import AIClient
from ai.providers import build_providers
from bot.handlers import get_routers
from bot.middlewares.access import AccessMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from config.settings import ConfigError, Settings, load_settings
from database.session import (
    create_engine,
    create_session_factory,
    describe_database,
    init_db,
)
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from bot.handlers.reminders import make_sender
from services.longterm import LongTermMemory
from services.speech import SpeechToText
from services.reminders import ReminderScheduler, Reminders
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.usage import UsageTracker
from web.server import create_web_app, start_web_server

logger = logging.getLogger("assistant")

BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск и приветствие"),
    BotCommand(command="help", description="Что я умею"),
    BotCommand(command="reminders", description="Мои напоминания"),
    BotCommand(command="timezone", description="Часовой пояс для напоминаний"),
    BotCommand(command="memory", description="Что я о тебе помню"),
    BotCommand(command="forget", description="Забыть всё, что я о тебе помню"),
    BotCommand(command="clear", description="Очистить историю диалога"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


def create_dispatcher(
    settings: Settings,
    ai_client: AIClient,
    memory: MemoryManager,
    usage: UsageTracker,
    *,
    chat_service: ChatService | None = None,
    limiter: SlidingWindowLimiter | None = None,
    long_memory: LongTermMemory | None = None,
    reminders: Reminders | None = None,
    speech: SpeechToText | None = None,
) -> Dispatcher:
    """chat_service и limiter передаются снаружи, когда их делит с ботом Mini App."""
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["ai_client"] = ai_client
    dp["memory"] = memory
    dp["usage"] = usage
    dp["long_memory"] = long_memory
    dp["reminders"] = reminders
    dp["speech"] = speech
    dp["chat_service"] = chat_service or ChatService(settings, ai_client, memory)

    dp.update.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.message.middleware(
        ThrottlingMiddleware(limiter or SlidingWindowLimiter(settings.rate_limit_per_minute))
    )
    dp.include_routers(*get_routers())
    return dp


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # HTTPX на уровне INFO логирует каждый URL запроса — это лишнее.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def setup_menu_button(bot: Bot, settings: Settings) -> None:
    """Кнопка меню рядом с полем ввода открывает Mini App.

    Если Mini App выключен, кнопка не трогается: её могли настроить вручную в @BotFather.
    """
    if not settings.webapp_enabled:
        return
    button = MenuButtonWebApp(
        text=settings.assistant_name, web_app=WebAppInfo(url=settings.webapp_url)
    )
    try:
        await bot.set_chat_menu_button(menu_button=button)
    except TelegramAPIError as exc:
        # Бот остаётся рабочим и без кнопки: Mini App открывается из /start.
        logger.warning("Could not set the menu button: %s", exc)


async def run(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    await init_db(engine)
    logger.info("Database: %s", describe_database(settings.database_url))
    session_factory = create_session_factory(engine)

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
        headers={"User-Agent": "telegram-ai-assistant/1.0"},
    )
    usage = UsageTracker(session_factory, settings.daily_request_limit)
    ai_client = AIClient(
        build_providers(settings.providers, http_client),
        max_tokens=settings.max_output_tokens,
        temperature=settings.temperature,
        usage=usage,
    )
    memory = MemoryManager(
        MessageRepository(session_factory, store_limit=settings.history_store_limit),
        history_limit=settings.history_max_messages,
    )

    preferences = PreferencesStore(session_factory)
    long_memory = LongTermMemory(session_factory, ai_client)
    reminders = Reminders(session_factory, ai_client)
    speech = (
        SpeechToText(settings.speech_api_key, http_client, model=settings.speech_model)
        if settings.speech_api_key
        else None
    )
    chat_service = ChatService(settings, ai_client, memory, preferences, long_memory, reminders)
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)

    bot = Bot(token=settings.bot_token)
    dp = create_dispatcher(
        settings,
        ai_client,
        memory,
        usage,
        chat_service=chat_service,
        limiter=limiter,
        long_memory=long_memory,
        reminders=reminders,
        speech=speech,
    )
    web_runner = None
    scheduler_task = None

    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.error("Telegram rejected BOT_TOKEN: check the token from @BotFather")
            raise SystemExit(1) from None
        except TelegramNetworkError as exc:
            logger.error("Cannot reach Telegram API: %s", exc)
            raise SystemExit(1) from None
        await bot.set_my_commands(BOT_COMMANDS)
        await setup_menu_button(bot, settings)
        if settings.webapp_enabled:
            try:
                web_runner = await start_web_server(
                    create_web_app(
                        settings,
                        chat_service,
                        memory,
                        usage,
                        ai_client,
                        limiter,
                        preferences,
                        long_memory,
                        reminders,
                        speech,
                    ),
                    settings.webapp_host,
                    settings.webapp_port,
                )
            except OSError as exc:
                logger.error(
                    "Cannot start the Mini App server on %s:%s: %s",
                    settings.webapp_host,
                    settings.webapp_port,
                    exc,
                )
                raise SystemExit(1) from None
        logger.info(
            "Bot @%s started. Providers: %s. Mini App: %s",
            me.username,
            ", ".join(f"{p.name}[{','.join(p.models)}]" for p in settings.providers),
            settings.webapp_url or "off (set WEBAPP_URL to enable)",
        )
        scheduler = ReminderScheduler(reminders, make_sender(bot))
        scheduler_task = asyncio.create_task(scheduler.run(), name="reminders")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        if web_runner is not None:
            await web_runner.cleanup()
        await http_client.aclose()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(settings.log_level)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
