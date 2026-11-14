from aiogram import Router

from bot.handlers import chat, commands, draw, errors, reminders, study


def get_routers() -> list[Router]:
    """Порядок важен: учебные сценарии (у них свои состояния и команды) раньше остальных,
    команды — раньше общего обработчика текста."""
    return [
        errors.create_router(),
        study.create_router(),
        reminders.create_router(),
        draw.create_router(),
        chat.create_search_router(),
        commands.create_router(),
        chat.create_router(),
    ]
