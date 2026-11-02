"""Точка входа: сборка зависимостей и запуск бота (long polling)."""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from ai.client import AIClient
from ai.providers import build_providers
from bot.handlers import get_routers
from bot.middlewares.access import AccessMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from config.settings import ConfigError, Settings, load_settings
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.usage import UsageTracker

logger = logging.getLogger("assistant")

BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск и приветствие"),
    BotCommand(command="help", description="Что я умею"),
    BotCommand(command="clear", description="Очистить историю диалога"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


def create_dispatcher(
    settings: Settings, ai_client: AIClient, memory: MemoryManager, usage: UsageTracker
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["ai_client"] = ai_client
    dp["memory"] = memory
    dp["usage"] = usage

    dp.update.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.message.middleware(ThrottlingMiddleware(settings.rate_limit_per_minute))
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


async def run(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    await init_db(engine)
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

    bot = Bot(token=settings.bot_token)
    dp = create_dispatcher(settings, ai_client, memory, usage)

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
        logger.info(
            "Bot @%s started. Providers: %s",
            me.username,
            ", ".join(f"{p.name}[{','.join(p.models)}]" for p in settings.providers),
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
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
