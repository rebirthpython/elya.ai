"""Команды: /start, /help, /code, /models, /clear, /cancel и неизвестные команды."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ai.client import AIClient
from bot.handlers.chat import send_reply
from bot.keyboards.common import (
    ClearHistoryCallback,
    CodeModeCallback,
    InstructionsCallback,
    instructions_keyboard,
    MemoryCallback,
    clear_confirm_keyboard,
    code_mode_keyboard,
    forget_confirm_keyboard,
    memory_keyboard,
    open_app_keyboard,
)
from config.settings import Settings
from memory.manager import MemoryManager
from services.chat import ChatService
from services.instructions import Instructions, InstructionsStore
from services.longterm import LongTermMemory
from services.modes import CHAT, CODE, ModeStore
from services.search import WebSearch
from services.usage import UsageTracker


def _privacy_note(ai_client: AIClient, web_search: WebSearch | None = None) -> str:
    providers = escape(", ".join(ai_client.provider_titles))
    search_note = (
        f"Поисковые запросы, которые я составляю для ответа, уходят в <i>"
        f"{escape(', '.join(web_search.provider_titles))}</i>. "
        if web_search
        else ""
    )
    return (
        "🔒 <b>О приватности.</b> Твои сообщения и недавняя история диалога отправляются "
        f"для ответа во внешний ИИ-сервис: <i>{providers}</i>. {search_note}На бесплатных тарифах "
        "провайдеры могут использовать данные для улучшения своих моделей, поэтому "
        "не присылай пароли, номера карт и другие секреты. История хранится только в базе "
        "этого бота, её можно удалить командой /clear."
    )


def _search_start(web_search: WebSearch) -> str:
    when = "когда нужны свежие сведения, я загляну в интернет" if web_search.auto else "загляну в интернет"
    return (
        f"🔎 Могу поискать в интернете: {when} и приложу ссылки на источники. "
        "Попроси «погугли …» или используй /search.\n\n"
    )


async def cmd_start(
    message: Message,
    state: FSMContext,
    settings: Settings,
    ai_client: AIClient,
    web_search: WebSearch | None = None,
) -> None:
    await state.clear()
    first_name = message.from_user.first_name if message.from_user else ""
    greeting = f"Привет, {escape(first_name)}!" if first_name else "Привет!"
    app_note = (
        "У меня есть удобное приложение прямо в Telegram — открой его кнопкой ниже "
        "или пиши здесь, память у нас общая.\n\n"
        if settings.webapp_enabled
        else ""
    )
    await message.answer(
        f"{greeting} Я — {escape(settings.assistant_name)}, твой персональный ИИ-ассистент. 👋\n\n"
        "Просто пиши мне обычными сообщениями: могу ответить на вопрос, помочь с учёбой, "
        "спланировать день или просто поболтать. Я помню контекст нашего разговора.\n\n"
        f"{app_note}"
        "🎨 Напиши «нарисуй …» или /draw — нарисую картинку, а пришли фото с подписью «убери фон» — изменю его.\n"
        "📄 Пришли PDF, Word или таблицу — найду ответ и укажу страницы, а два файла сравню.\n"
        "🗂 /new — новый чат, /chats — прошлые чаты, /projects — проекты «Учёба», «Код», «Личное» "
        "со своими инструкциями и файлами.\n"
        "📝 /notes — сделаю конспект или сокращу лекцию, 🎞 /slides — соберу презентацию, "
        "🎬 /video — смонтирую видео (или просто пришли его мне), "
        "👩‍💻 /code — режим кода: пишу программы и ищу ошибки.\n\n"
        f"{_search_start(web_search) if web_search else ''}"
        f"{_privacy_note(ai_client, web_search)}\n\n"
        "Все команды — в /help.",
        parse_mode=ParseMode.HTML,
        reply_markup=(
            open_app_keyboard(settings.webapp_url, settings.assistant_name)
            if settings.webapp_enabled
            else None
        ),
    )


async def cmd_help(
    message: Message,
    settings: Settings,
    memory: MemoryManager,
    usage: UsageTracker,
    web_search: WebSearch | None = None,
) -> None:
    used = await usage.used_today()
    limit = str(usage.daily_limit) if usage.daily_limit else "∞"
    history = await memory.history_size(message.from_user.id) if message.from_user else 0
    if web_search:
        search_limit = str(web_search.daily_limit) if web_search.daily_limit else "∞"
        search_stats = f"🔎 Поисков в интернете сегодня: {await web_search.used_today()} из {search_limit}\n"
        search_line = "🔎 Искать в интернете и давать ссылки на источники\n"
    else:
        search_stats = search_line = ""
    await message.answer(
        f"<b>Что умеет {escape(settings.assistant_name)}</b>\n\n"
        "💬 Отвечать на вопросы и поддерживать диалог с учётом контекста\n"
        "📚 Помогать с учёбой: объяснить тему, проверить текст, составить план\n"
        "📝 Делать конспекты и сокращать лекции, выделяя термины, даты и имена\n"
        "🎞 Делать презентации с анимациями и заметками докладчика\n"
        "🎬 Монтировать видео: обрезать, склеить, ускорить, убрать паузы, субтитры, вертикальный формат\n"
        "👩‍💻 Программировать: писать и объяснять код, искать ошибки, разбирать файлы\n"
        "📄 Читать документы (PDF, Word, Excel, CSV): находить ответ со ссылками на страницы и сравнивать файлы\n"
        "🔗 Разбирать ссылки: пришли статью — выделю главное и отвечу по ней; у видео — по субтитрам\n"
        "🗂 Вести отдельные чаты и проекты — у проекта свои инструкции и файлы\n"
        "🐙 Работать с GitHub: отправлять написанный код в твой репозиторий и читать оттуда файлы\n"
        "🎨 Рисовать картинки: просто напиши «нарисуй …»\n"
        "✏️ Редактировать картинки: фото с подписью «убери фон», «сделай зимним» или /edit\n"
        f"{search_line}"
        "🗓 Помогать с планированием и организацией дел\n\n"
        "<b>Команды</b>\n"
        "/notes — конспект, краткая выжимка или главные детали из лекции\n"
        "/slides — презентация (.pptx) по теме или материалу\n"
        "/video — монтаж видео (можно и просто прислать видео)\n"
        "/code — режим кода (выключить: /code off)\n"
        "/draw — нарисовать картинку по описанию\n"
        "/edit — изменить картинку (ответом на неё или последнюю)\n"
        "/search — найти в интернете и ответить со ссылками на источники\n"
        "/new — новый чат без старой переписки\n"
        "/chats — мои чаты: вернуться к прошлому\n"
        "/projects — проекты (папки чатов); <code>/project Название</code> — перейти или создать\n"
        "/docs — документы в чате (пришли файл, чтобы добавить)\n"
        "/github — подключить GitHub; /repo — репозиторий; /push — отправить файлы; /pull файл — показать мне файл\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/models — какие нейросети подключены\n"
        "/reminders — мои напоминания (ставятся фразой «напомни завтра в 9 …»)\n"
        "/timezone — часовой пояс для напоминаний\n"
        "/instructions — твои инструкции для меня (как в ChatGPT)\n"
        "/memory — что я о тебе помню (и включить или выключить память)\n"
        "/forget — забыть всё, что я о тебе помню\n"
        "/clear — очистить этот чат\n"
        "/cancel — отменить текущее действие\n\n"
        f"📊 Сообщений в этом чате: {history}\n"
        f"📈 Запросов к ИИ сегодня (UTC): {used} из {limit}\n"
        f"{search_stats}\n"
        "<i>Скоро: долговременная память, настройки стиля, напоминания, голосовые и фото.</i>",
        parse_mode=ParseMode.HTML,
    )


# Telegram принимает до 4096 символов в сообщении; оставляем запас на разметку.
_MODELS_CHUNK = 3800


def _models_blocks(ai_client: AIClient) -> list[str]:
    total = len(ai_client.available_models)
    providers = ai_client.providers
    blocks = [
        f"🧠 <b>Подключённые нейросети:</b> {total} у {len(providers)} провайдеров.\n"
        "Порядок — это приоритет: если модель недоступна или упёрлась в лимит, "
        "отвечает следующая. 🖼 — понимает фото, ⏸ — временно пропускается после ошибки."
    ]
    for provider in providers:
        provider_paused = ai_client.is_paused(provider.name)
        lines = [f"<b>{escape(provider.title)}</b>" + (" ⏸" if provider_paused else "")]
        for model in provider.models:
            paused = not provider_paused and ai_client.is_paused(provider.name, model)
            vision = " 🖼" if model in provider.vision_models else ""
            lines.append(f"{'⏸' if paused else '•'} <code>{escape(model)}</code>{vision}")
        photo_only = [m for m in provider.vision_models if m not in provider.models]
        if photo_only:
            lines.append("🖼 для фото: " + ", ".join(f"<code>{escape(m)}</code>" for m in photo_only))
        blocks.append("\n".join(lines))
    blocks.append(
        "<i>Чтобы подключить ещё, впиши бесплатный ключ провайдера в .env "
        "и перезапусти бота — список с ссылками есть в .env.example.</i>"
    )
    return blocks


async def cmd_models(message: Message, ai_client: AIClient) -> None:
    chunks: list[str] = []
    for block in _models_blocks(ai_client):
        if chunks and len(chunks[-1]) + len(block) + 2 <= _MODELS_CHUNK:
            chunks[-1] += "\n\n" + block
        else:
            chunks.append(block)
    for chunk in chunks:
        await message.answer(chunk, parse_mode=ParseMode.HTML)


_MEMORY_OFF = (
    "🧠 <b>Долговременная память выключена.</b>\n\n"
    "Если включить, я буду запоминать важное о тебе (имя, близких, вкусы, планы) и помнить "
    "это между разговорами. Я не сохраняю пароли и номера карт, а всё запомненное можно "
    "посмотреть и удалить в любой момент. Данные хранятся только в базе этого бота, но при "
    "ответах они, как и история диалога, отправляются во внешний ИИ-сервис."
)


async def _memory_text(memory: LongTermMemory, user_id: int) -> tuple[str, bool, bool]:
    enabled = await memory.is_enabled(user_id)
    facts = await memory.list(user_id)
    if not enabled and not facts:
        return _MEMORY_OFF, False, False
    status = "включена" if enabled else "выключена"
    if facts:
        body = "\n".join(f"• {escape(fact.text)}" for fact in facts)
    else:
        body = "<i>Пока ничего не запомнила. Расскажи что-нибудь о себе.</i>"
    text = f"🧠 <b>Память {status}.</b> Вот что я о тебе помню:\n\n{body}"
    if not enabled:
        text += "\n\n<i>Пока память выключена, я этим не пользуюсь и ничего нового не запоминаю.</i>"
    return text, enabled, bool(facts)


async def cmd_memory(message: Message, long_memory: LongTermMemory | None) -> None:
    if long_memory is None or message.from_user is None:
        await message.answer("Долговременная память сейчас недоступна.")
        return
    text, enabled, has_facts = await _memory_text(long_memory, message.from_user.id)
    await message.answer(
        text, parse_mode=ParseMode.HTML, reply_markup=memory_keyboard(enabled, has_facts)
    )


async def cmd_forget(message: Message, long_memory: LongTermMemory | None) -> None:
    if long_memory is None or message.from_user is None:
        await message.answer("Долговременная память сейчас недоступна.")
        return
    if not await long_memory.count(message.from_user.id):
        await message.answer("Мне пока нечего забывать: я ничего о тебе не храню.")
        return
    await message.answer(
        "Забыть всё, что я о тебе помню? Это нельзя отменить.",
        reply_markup=forget_confirm_keyboard(),
    )


async def on_memory_action(
    callback: CallbackQuery, callback_data: MemoryCallback, long_memory: LongTermMemory | None
) -> None:
    if long_memory is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user_id = callback.from_user.id
    if callback_data.action == "toggle":
        await long_memory.set_enabled(user_id, not await long_memory.is_enabled(user_id))
    elif callback_data.action == "forget_ask":
        await callback.message.edit_text(
            "Забыть всё, что я о тебе помню? Это нельзя отменить.",
            reply_markup=forget_confirm_keyboard(),
        )
        await callback.answer()
        return
    elif callback_data.action == "forget_yes":
        deleted = await long_memory.clear(user_id)
        await callback.answer(f"Забыла: {deleted}")
    text, enabled, has_facts = await _memory_text(long_memory, user_id)
    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=memory_keyboard(enabled, has_facts)
    )
    await callback.answer()


def _instructions_text(instructions: Instructions, has_app: bool) -> str:
    if instructions.is_empty:
        where = (
            "Открой приложение → ⋯ → «Инструкции»"
            if has_app
            else "Их можно задать в приложении (⋯ → «Инструкции»)"
        )
        return (
            "✍️ <b>Инструкции не заданы.</b>\n\n"
            "Здесь можно один раз написать, что мне знать о тебе и как отвечать, — и я буду "
            "учитывать это в каждом разговоре, чтобы не повторять одно и то же. " + where + "."
        )
    status = "включены" if instructions.enabled else "выключены — я их сейчас не учитываю"
    parts = [f"✍️ <b>Инструкции {status}.</b>"]
    if instructions.about:
        parts.append(f"<b>Что знать о тебе:</b>\n{escape(instructions.about)}")
    if instructions.style:
        parts.append(f"<b>Как отвечать:</b>\n{escape(instructions.style)}")
    return "\n\n".join(parts)


async def cmd_instructions(
    message: Message, settings: Settings, instructions: InstructionsStore | None = None
) -> None:
    if instructions is None or message.from_user is None:
        await message.answer("Инструкции сейчас недоступны.")
        return
    current = await instructions.get(message.from_user.id)
    await message.answer(
        _instructions_text(current, settings.webapp_enabled),
        parse_mode=ParseMode.HTML,
        reply_markup=instructions_keyboard(current.enabled, not current.is_empty, settings.webapp_url),
    )


async def on_instructions_action(
    callback: CallbackQuery,
    callback_data: InstructionsCallback,
    settings: Settings,
    instructions: InstructionsStore | None = None,
) -> None:
    if instructions is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    current = await instructions.get(callback.from_user.id)
    if callback_data.action == "toggle":
        current = await instructions.update(callback.from_user.id, enabled=not current.enabled)
    await callback.message.edit_text(
        _instructions_text(current, settings.webapp_enabled),
        parse_mode=ParseMode.HTML,
        reply_markup=instructions_keyboard(current.enabled, not current.is_empty, settings.webapp_url),
    )
    await callback.answer("Включены" if current.enabled else "Выключены")


_CODE_OFF_WORDS = frozenset({"off", "выкл", "выключи", "выключить", "stop", "стоп", "exit", "выход"})
_CODE_ON_WORDS = frozenset({"on", "вкл", "включи", "включить"})
_CODE_MODE_OFF = (
    "💬 <b>Режим кода выключен.</b> Снова общаемся как обычно. Включить его можно командой /code."
)


def _code_mode_text(already: bool, sees_photos: bool) -> str:
    title = "Режим кода уже включён." if already else "Режим кода включён."
    screenshot = "• Скриншот ошибки тоже разберу\n" if sees_photos else ""
    return (
        f"👩‍💻 <b>{title}</b>\n\n"
        "Теперь я отвечаю как программист: пишу код, объясняю, ищу ошибки и делаю ревью.\n\n"
        "• Опиши задачу или пришли код с вопросом\n"
        "• Пришли файл с кодом (.py, .js, .json…) — прочитаю его и отвечу на подпись\n"
        f"{screenshot}"
        "• Длинный код пришлю ещё и файлом\n\n"
        "Режим общий для чата и приложения. Вернуться к обычному общению — /code off "
        "или кнопка ниже.\n\n"
        "<i>Я не запускаю код, поэтому проверяй его у себя, прежде чем использовать.</i>"
    )


async def cmd_code(
    message: Message,
    command: CommandObject,
    bot: Bot,
    chat_service: ChatService,
    ai_client: AIClient,
    modes: ModeStore | None = None,
) -> None:
    """/code — включить режим кода, /code off — выключить, /code <задача> — включить и сразу ответить."""
    if modes is None or message.from_user is None:
        await message.answer("Режим кода сейчас недоступен.")
        return
    user_id = message.from_user.id
    task = (command.args or "").strip()
    already = await modes.get(user_id) == CODE

    if task.lower() in _CODE_OFF_WORDS:
        await modes.set(user_id, CHAT)
        await message.answer(
            _CODE_MODE_OFF if already else "Режим кода и так выключен. Включить его — /code.",
            parse_mode=ParseMode.HTML,
            reply_markup=code_mode_keyboard(False),
        )
        return

    await modes.set(user_id, CODE)
    if task and task.lower() not in _CODE_ON_WORDS:
        if not already:
            await message.answer("👩‍💻 Режим кода включён. Вернуться к обычному общению — /code off.")
        await send_reply(message, bot, chat_service, task)
        return
    await message.answer(
        _code_mode_text(already, ai_client.has_vision),
        parse_mode=ParseMode.HTML,
        reply_markup=code_mode_keyboard(True),
    )


async def on_code_mode_action(
    callback: CallbackQuery,
    callback_data: CodeModeCallback,
    ai_client: AIClient,
    modes: ModeStore | None = None,
) -> None:
    if modes is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    enabled = callback_data.action == "on"
    await modes.set(callback.from_user.id, CODE if enabled else CHAT)
    await callback.message.edit_text(
        _code_mode_text(False, ai_client.has_vision) if enabled else _CODE_MODE_OFF,
        parse_mode=ParseMode.HTML,
        reply_markup=code_mode_keyboard(enabled),
    )
    await callback.answer("Режим кода включён" if enabled else "Обычный режим")


async def cmd_clear(message: Message) -> None:
    await message.answer(
        "Очистить этот чат — его переписку и файлы? Другие чаты и проекты останутся. Это действие нельзя отменить.",
        reply_markup=clear_confirm_keyboard(),
    )


async def on_clear_confirm(
    callback: CallbackQuery,
    callback_data: ClearHistoryCallback,
    memory: MemoryManager,
    chat_service: ChatService | None = None,
) -> None:
    if callback_data.confirm:
        deleted = await memory.clear_history(callback.from_user.id)  # и файлы этого чата
        text = f"🗑 Чат очищен (удалено сообщений: {deleted}). Начнём с чистого листа!"
    else:
        text = "Хорошо, история сохранена."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text)
    await callback.answer()


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Действие отменено.")


async def cmd_unknown(message: Message) -> None:
    await message.answer("Такой команды я пока не знаю. Список команд — /help")


def create_router() -> Router:
    router = Router(name="commands")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_code, Command("code"))
    router.message.register(cmd_models, Command("models"))
    router.message.register(cmd_instructions, Command("instructions"))
    router.message.register(cmd_memory, Command("memory"))
    router.message.register(cmd_forget, Command("forget"))
    router.message.register(cmd_clear, Command("clear"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.callback_query.register(on_clear_confirm, ClearHistoryCallback.filter())
    router.callback_query.register(on_memory_action, MemoryCallback.filter())
    router.callback_query.register(on_instructions_action, InstructionsCallback.filter())
    router.callback_query.register(on_code_mode_action, CodeModeCallback.filter())
    router.message.register(cmd_unknown, F.text.startswith("/"))
    return router
