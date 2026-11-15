"""/run — запустить Python в песочнице без участия модели: код после команды или ответом
на сообщение с кодом. Без кода — что умеет песочница и как она изолирована."""

from __future__ import annotations

import re
from html import escape

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from bot.handlers.chat import send_output_files
from bot.utils.sender import send_long_text
from services.chat import ChatService
from services.code import code_blocks
from services.coderun import display_block, output_files

_PYTHON = {"", "python", "python3", "py"}
_FENCE_RE = re.compile(r"^\s*```[\w+#. -]*\n(.*?)\n?```\s*$", re.DOTALL)


def code_to_run(text: str) -> str:
    """Python из текста: последний блок ```python, иначе сам текст (без ограды, если она есть)."""
    blocks = [block for block in code_blocks(text) if block.language in _PYTHON or "run" in block.language]
    if blocks:
        return blocks[-1].code
    match = _FENCE_RE.match(text)
    return (match.group(1) if match else text).strip("\n")


def _status_text(chat_service: ChatService) -> str:
    sandbox = chat_service.sandbox
    if sandbox is None:
        return (
            "▶️ <b>Запуск кода выключен.</b>\n\n"
            "Владелец бота может включить его в .env: <code>SANDBOX_BACKEND=auto</code> "
            "(или docker / bwrap / process — подробности в README, раздел «Запуск кода»)."
        )
    return (
        "▶️ <b>Песочница для кода</b>\n\n"
        f"{escape(sandbox.describe())}.\n\n"
        "В режиме кода (/code) и когда нужно посчитать или построить график, я сама запускаю "
        "Python, смотрю на вывод и исправляю ошибки. Можно и напрямую:\n"
        "<code>/run print(2 ** 100)</code>\n"
        "или ответь командой /run на сообщение с кодом.\n\n"
        "Код работает отдельно от бота: без интернета, без ключей и файлов бота, "
        "с лимитами времени и памяти."
    )


async def cmd_run(message: Message, command: CommandObject, chat_service: ChatService) -> None:
    if message.from_user is None:
        return
    source = (command.args or "").strip()
    reply = message.reply_to_message
    if not source and reply is not None:
        source = (reply.text or reply.caption or "").strip()
    sandbox = chat_service.sandbox
    if not source or sandbox is None:
        await message.answer(_status_text(chat_service), parse_mode=ParseMode.HTML)
        return
    code = code_to_run(source)
    if not code.strip():
        await message.answer("Не нашла кода, который можно запустить 🙂")
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        result = await sandbox.run(code)
    await send_long_text(message, display_block(result).strip() + f"\n\n_({result.duration:.1f} с)_")
    await send_output_files(message, tuple((output, None) for output in output_files(result.files)), None)


def create_router() -> Router:
    router = Router(name="sandbox")
    router.message.register(cmd_run, Command("run", "exec", "python"))
    return router
