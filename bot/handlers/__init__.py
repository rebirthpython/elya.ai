from aiogram import Router

from bot.handlers import chat, chats, commands, documents, draw, errors, github, reminders, sandbox, study, video


def get_routers() -> list[Router]:
    """Порядок важен: учебные сценарии и монтаж (у них свои состояния и команды) раньше
    остальных, команды — раньше общего обработчика текста."""
    return [
        errors.create_router(),
        study.create_router(),
        video.create_router(),
        reminders.create_router(),
        draw.create_router(),
        chat.create_search_router(),
        documents.create_router(),
        chats.create_router(),
        github.create_router(),
        sandbox.create_router(),
        commands.create_router(),
        chat.create_router(),
    ]
