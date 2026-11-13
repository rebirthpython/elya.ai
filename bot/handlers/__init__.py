from aiogram import Router

from bot.handlers import chat, commands, errors, reminders


def get_routers() -> list[Router]:
    """Порядок важен: команды раньше общего обработчика текста."""
    return [
        errors.create_router(),
        reminders.create_router(),
        commands.create_router(),
        chat.create_router(),
    ]
