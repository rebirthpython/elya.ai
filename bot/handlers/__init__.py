from aiogram import Router

from bot.handlers import chat, commands, errors, study


def get_routers() -> list[Router]:
    """Порядок важен: учебные сценарии (у них свои состояния и команды) раньше общих команд,
    команды — раньше общего обработчика текста."""
    return [
        errors.create_router(),
        study.create_router(),
        commands.create_router(),
        chat.create_router(),
    ]
