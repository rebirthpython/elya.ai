"""GitHub в чате с ботом: /github (подключить токен), /repo (выбрать или создать репозиторий),
/push (отправить файлы из ответа), /pull (показать Эле файл из репозитория) и кнопка
«Отправить в GitHub» под ответом с файлами."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from services.chat import ChatService
from services.code import CodeFile, language_for
from services.github import (
    FINE_GRAINED_URL,
    TOKEN_URL,
    GitHubError,
    GitHubService,
    commit_message,
    find_token,
)

logger = logging.getLogger(__name__)

# Последний список репозиториев для кнопок (в callback_data имя целиком не помещается).
_repo_choices: dict[int, list[str]] = {}

HOW_TO_CONNECT = (
    "🐙 <b>GitHub</b>\n\n"
    "Подключи свой GitHub — и я смогу отправлять написанный код прямо в репозиторий "
    "одним коммитом и читать оттуда файлы.\n\n"
    "1. Создай токен: <a href=\"{classic}\">classic со scope <code>repo</code></a> (проще) или "
    "<a href=\"{fine}\">fine-grained</a> с правами <i>Contents: Read and write</i> "
    "(и <i>Administration: Read and write</i>, чтобы я могла создавать репозитории).\n"
    "2. Пришли его сюда: <code>/github ghp_…</code> — я проверю токен, сохраню его "
    "зашифрованным и сразу удалю твоё сообщение с ним.\n"
    "3. Выбери репозиторий: /repo — или создай новый: <code>/repo new my-telegram-bot</code>."
)


class GitHubCallback(CallbackData, prefix="gh"):
    action: str  # push | repo | new
    id: int = 0


def _push_keyboard(bundle_id: int, repo: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"⬆️ Отправить в {repo}"[:60], callback_data=GitHubCallback(action="push", id=bundle_id).pack())
    ]])


async def offer_push(message: Message, github: GitHubService | None, bundle_id: int | None) -> None:
    """Под ответом с файлами — кнопка «Отправить в GitHub»."""
    if github is None or bundle_id is None:
        return
    bundle = github.bundle(message.chat.id, bundle_id)
    status = await github.status(message.chat.id)
    if bundle is None or not status.repo:
        return
    paths = ", ".join(path for path, _ in bundle.files[:6]) + (" …" if len(bundle.files) > 6 else "")
    await message.answer(
        f"🐙 Отправить в GitHub ({escape(status.repo)}, ветка {escape(status.branch or 'main')})?\n"
        f"<i>{escape(paths)}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_push_keyboard(bundle_id, status.repo),
    )


def _status_text(status, paths: list[str] | None = None) -> str:
    if not status.connected:
        return HOW_TO_CONNECT.format(classic=TOKEN_URL, fine=FINE_GRAINED_URL)
    lines = [f"🐙 <b>GitHub подключён</b>{f' как <b>{escape(status.login)}</b>' if status.login else ''}."]
    if status.repo:
        lines.append(f"Репозиторий: <a href=\"https://github.com/{escape(status.repo)}\">{escape(status.repo)}</a>, "
                     f"ветка <code>{escape(status.branch or 'main')}</code>.")
        if paths is not None:
            shown = ", ".join(escape(p) for p in paths[:25]) + (" …" if len(paths) > 25 else "")
            lines.append(f"Файлы: {shown}" if paths else "Репозиторий пока пустой.")
    else:
        lines.append("Репозиторий не выбран — /repo")
    lines.append(
        "\nКак работать: попроси написать код (лучше в проекте «Код») — под ответом с файлами "
        "появится кнопка «Отправить в GitHub». Ещё: /push — отправить последние файлы, "
        "<code>/pull bot.py</code> — показать мне файл из репозитория, /repo — сменить репозиторий"
        + ("." if status.from_env else ", <code>/github off</code> — отключить.")
    )
    return "\n".join(lines)


async def _connect(message: Message, bot: Bot, github: GitHubService, token: str) -> None:
    # Токен — секрет: сообщение с ним убираем из переписки сразу.
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except TelegramAPIError:
        pass
    try:
        status = await github.connect(message.from_user.id, token)
    except GitHubError as exc:
        await message.answer(f"🐙 {exc}")
        return
    await message.answer(
        f"✅ GitHub подключён как <b>{escape(status.login)}</b>. Сообщение с токеном я удалила.\n\n"
        "Теперь выбери репозиторий: /repo — или создай новый: <code>/repo new my-telegram-bot</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_github(message: Message, command: CommandObject, bot: Bot, chat_service: ChatService) -> None:
    github: GitHubService | None = chat_service.github
    if github is None or message.from_user is None:
        return
    args = (command.args or "").strip()
    user_id = message.from_user.id
    if token := find_token(args):
        await _connect(message, bot, github, token)
        return
    if args.lower() in ("off", "отключить", "выключить", "disconnect"):
        await github.disconnect(user_id)
        await message.answer("🐙 GitHub отключён, токен удалён из базы.")
        return
    status = await github.status(user_id)
    paths = None
    if status.repo:
        try:
            paths = await github.tree(user_id)
        except GitHubError as exc:
            await message.answer(f"🐙 {exc}")
    await message.answer(_status_text(status, paths), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_token_message(message: Message, bot: Bot, chat_service: ChatService) -> None:
    """Токен прислали без команды — не отдаём его модели, а подключаем."""
    github: GitHubService | None = chat_service.github
    token = find_token(message.text or "")
    if github is None or token is None:
        return
    await _connect(message, bot, github, token)


async def cmd_repo(message: Message, command: CommandObject, chat_service: ChatService) -> None:
    github: GitHubService | None = chat_service.github
    if github is None or message.from_user is None:
        return
    user_id = message.from_user.id
    args = (command.args or "").strip()
    try:
        if args:
            first, _, rest = args.partition(" ")
            if first.lower() in ("new", "create", "создай", "создать", "новый") and rest.strip():
                status = await github.create_repo(user_id, rest.strip(), description="Сделано вместе с Элей")
                await message.answer(
                    f"✅ Создала приватный репозиторий <a href=\"https://github.com/{escape(status.repo)}\">"
                    f"{escape(status.repo)}</a> и выбрала его. Попроси написать код — и отправим его туда.",
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
                return
            status = await github.use_repo(user_id, args)
            await message.answer(
                f"✅ Работаем с <b>{escape(status.repo)}</b> (ветка <code>{escape(status.branch)}</code>).",
                parse_mode=ParseMode.HTML,
            )
            return
        repos = await github.repos(user_id)
    except GitHubError as exc:
        await message.answer(f"🐙 {exc}")
        return
    _repo_choices[user_id] = repos
    rows = [
        [InlineKeyboardButton(text=name, callback_data=GitHubCallback(action="repo", id=index).pack())]
        for index, name in enumerate(repos)
    ]
    rows.append([InlineKeyboardButton(text="➕ Новый репозиторий", callback_data=GitHubCallback(action="new").pack())])
    await message.answer(
        "🐙 Выбери репозиторий (недавние) или напиши <code>/repo владелец/имя</code>.\n"
        "Новый: <code>/repo new my-telegram-bot</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _push(message: Message, github: GitHubService, user_id: int, bundle_id: int | None, title: str | None = None) -> None:
    found = github.bundle(user_id, bundle_id) if bundle_id is not None else None
    if found is None:
        last = github.last_bundle(user_id)
        found = last[1] if last and bundle_id is None else None
    if found is None:
        await message.answer(
            "🐙 Не помню файлов для отправки (после перезапуска бота они забываются). "
            "Попроси Элю ещё раз прислать файлы целиком — с путями вроде «Файл `bot.py`:»."
        )
        return
    try:
        result = await github.push(user_id, list(found.files), title or commit_message(found.title))
    except GitHubError as exc:
        await message.answer(f"🐙 {exc}")
        return
    await message.answer(
        f"✅ Отправила в <b>{escape(result.repo)}</b> ({result.files} "
        f"{'файл' if result.files == 1 else 'файла' if 2 <= result.files <= 4 else 'файлов'}).\n"
        f"<a href=\"{result.commit_url}\">Коммит</a> · <a href=\"{result.tree_url}\">Репозиторий</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def cmd_push(message: Message, command: CommandObject, chat_service: ChatService) -> None:
    github: GitHubService | None = chat_service.github
    if github is None or message.from_user is None:
        return
    await _push(message, github, message.from_user.id, None, (command.args or "").strip() or None)


async def cmd_pull(message: Message, command: CommandObject, bot: Bot, chat_service: ChatService) -> None:
    """/pull путь [вопрос] — файл из репозитория попадает в разговор, как присланный файл."""
    github: GitHubService | None = chat_service.github
    if github is None or message.from_user is None:
        return
    path, _, question = (command.args or "").strip().partition(" ")
    if not path:
        await message.answer("Напиши путь к файлу: <code>/pull bot.py</code> — можно сразу с вопросом: "
                             "<code>/pull bot.py добавь команду /help</code>", parse_mode=ParseMode.HTML)
        return
    try:
        name, text = await github.read_file(message.from_user.id, path)
    except GitHubError as exc:
        await message.answer(f"🐙 {exc}")
        return
    limit = chat_service.settings.code_max_file_chars
    file = CodeFile(name=name, language=language_for(name), text=text[:limit], total_chars=len(text))
    from bot.handlers.chat import send_reply  # chat импортирует модули обработчиков

    await send_reply(message, bot, chat_service, question.strip() or "Вот файл из репозитория. Коротко скажи, что он делает, и жди задачу.", file=file)


async def on_github_action(callback: CallbackQuery, callback_data: GitHubCallback, chat_service: ChatService) -> None:
    github: GitHubService | None = chat_service.github
    message = callback.message if isinstance(callback.message, Message) else None
    if github is None or message is None:
        await callback.answer()
        return
    user_id = callback.from_user.id
    if callback_data.action == "push":
        await callback.answer("Отправляю…")
        await _push(message, github, user_id, callback_data.id)
    elif callback_data.action == "repo":
        choices = _repo_choices.get(user_id) or []
        if callback_data.id >= len(choices):
            await callback.answer("Список устарел — набери /repo ещё раз", show_alert=True)
            return
        try:
            status = await github.use_repo(user_id, choices[callback_data.id])
        except GitHubError as exc:
            await callback.answer(str(exc)[:190], show_alert=True)
            return
        await callback.answer("Готово")
        await message.answer(f"✅ Работаем с <b>{escape(status.repo)}</b> (ветка <code>{escape(status.branch)}</code>).",
                             parse_mode=ParseMode.HTML)
    else:
        await callback.answer()
        await message.answer("Напиши имя нового репозитория так: <code>/repo new my-telegram-bot</code>",
                             parse_mode=ParseMode.HTML)


def create_router() -> Router:
    router = Router(name="github")
    router.message.register(cmd_github, Command("github"))
    router.message.register(cmd_repo, Command("repo"))
    router.message.register(cmd_push, Command("push"))
    router.message.register(cmd_pull, Command("pull"))
    router.message.register(on_token_message, F.text.func(lambda text: find_token(text) is not None))
    router.callback_query.register(on_github_action, GitHubCallback.filter(F.action.in_({"push", "repo", "new"})))
    return router
