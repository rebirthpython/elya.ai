"""Рисование и правка картинок в диалоге: распознать просьбу, написать промпт, нарисовать
(или изменить присланную картинку), запомнить.

Правка: фото с подписью «убери фон», ответ на картинку «сделай её зимней» или просто
«а теперь добавь шляпу» сразу после картинки. Языковая модель решает, просьба ли это
изменить картинку, и пишет инструкцию по-английски; правит сервис из services.imagegen.

Просьбу («нарисуй кота в шляпе», /draw …) языковая модель превращает в подробный промпт
по-английски — так картинки получаются заметно лучше — с учётом недавнего диалога, поэтому
работают и уточнения вроде «а теперь то же самое, но зимой». Рисует отдельный сервис
(services.imagegen). В историю попадает подпись и промпт, сама картинка не хранится.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from ai.client import AIClient, AIError
from ai.providers.base import ChatMessage
from memory.manager import MemoryManager
from services.imagegen import ORIENTATIONS, GeneratedImage, ImageGenerator, SourceImage, prepare_source

logger = logging.getLogger(__name__)


# ---------- разбор ответа модели ----------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class ModelOutputError(Exception):
    """Ответ модели не удалось разобрать как JSON-объект."""


def extract_json_object(text: str) -> dict:
    """JSON-объект из ответа: модели часто оборачивают его в ``` или пишут текст вокруг."""
    candidates: list[str] = []
    if fence := _FENCE_RE.search(text):
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        for variant in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                data = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    raise ModelOutputError("ответ не является JSON-объектом")


def as_str(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return " ".join(value.split()) if isinstance(value, str) else ""


def shorten(text: str, limit: int) -> str:
    """Обрезает по границе слова с многоточием."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or text[: limit - 1]) + "…"

MAX_REQUEST = 1000
MAX_PROMPT = 1500
MAX_TITLE = 80
CONTEXT_MESSAGES = 6
CONTEXT_CHARS = 500
REMEMBER_DRAWINGS = 40  # сколько последних картинок можно перерисовать кнопкой «Ещё вариант»
AGAIN_TEXT = "🔄 Ещё вариант"
LAST_IMAGE_TTL = 3600.0  # «измени её» без фото относится к картинке не старше часа

# Просьба изменить картинку. Окончательно решает модель: «сделай конспект» она отличит.
EDIT_INTENT = re.compile(
    r"\b(?:"
    r"убер(?:и|ите)|убрать|удали(?:те)?|сотри|замени(?:те)?|поменяй(?:те)?|перекрас\w*|раскрас\w*|"
    r"добавь(?:те)?|дорису\w*|измени(?:те)?|изменить|отредактиру\w*|редактиру\w*|отретушир\w*|"
    r"ретушь|преврати(?:те)?|сделай(?:те)?|переделай(?:те)?|поставь|надень|одень|вставь|вырежи|"
    r"осветли|затемни|размой|улучши|подправь|исправь|верни|перенеси|"
    r"edit|remove|replace|add|change|turn|make)\b",
    re.IGNORECASE,
)

EDIT_WRITER = """\
Ты помогаешь редактировать картинки. У пользователя есть картинка (он её прислал или это \
последняя картинка в диалоге), и он что-то написал. Реши, просит ли он изменить эту \
картинку, и если да — напиши инструкцию для нейросети, которая правит картинки по тексту \
(FLUX Kontext / FLUX.2).

Ответь ТОЛЬКО JSON-объектом:
{"edit": true, "prompt": "...", "title": "..."}

Правила:
- prompt — по-английски, 1–3 фразы в повелительном наклонении: что именно изменить и что \
оставить как есть, например: "Replace the background with a sunny beach. Keep the person, \
their face, pose and clothing unchanged." Если не просят иного, сохраняй лица, позы, \
композицию и стиль. Надпись, которую просят добавить, дай в кавычках как есть.
- Если просьба ссылается на диалог («а теперь ещё и шляпу», «верни как было, но синим»), \
возьми подробности из недавних сообщений.
- title — коротко по-русски, 2–8 слов: что сделано, например «Фон заменён на пляж».
{decision_rule}
Никакого текста вне JSON.\
"""
EDIT_CLASSIFY_RULE = (
    "- Если сообщение — не просьба изменить картинку (вопрос о ней: что изображено, переведи "
    "или реши то, что на фото; просьба нарисовать что-то новое с нуля; просьба о другом, "
    "например «сделай конспект»), ответь {\"edit\": false}."
)
EDIT_EXPLICIT_RULE = "- Пользователь прямо попросил изменить картинку, поэтому edit всегда true."

_PICTURE_WORDS = (
    r"(?:картинк|изображени|рисун|иллюстраци|арт\b|аватар|обои|обоин|постер|плакат|логотип|"
    r"фото|пикч|эскиз|скетч|открытк)"
)
# Явная просьба нарисовать. Окончательно решает модель: «как научиться рисовать?» сюда
# не попадёт, а «нарисуй таблицу» модель отличит от просьбы о картинке.
INTENT = re.compile(
    r"\b(?:"
    r"нарису(?:й|йте|ешь|ете)|изобрази(?:те)?\b|"
    r"(?:сгенерируй|сгенери|создай|сделай|нарисуй|покажи|придумай|скинь|пришли)(?:те)?\s+"
    r"(?:\w+\s+){0,3}?" + _PICTURE_WORDS + r"|"
    r"(?:можешь|сможешь|могла\s+бы|можно)\s+(?:\w+\s+){0,2}?(?:нарисовать|изобразить|"
    r"сгенерировать\s+(?:\w+\s+)?" + _PICTURE_WORDS + r")|"
    r"draw\b|generate\s+(?:\w+\s+){0,2}?(?:image|picture)"
    r")",
    re.IGNORECASE,
)

PROMPT_WRITER = """\
Ты помогаешь рисовать картинки: превращаешь просьбу пользователя в промпт для нейросети, \
которая рисует по описанию (FLUX).

Ответь ТОЛЬКО JSON-объектом:
{"draw": true, "prompt": "...", "title": "...", "orientation": "square" | "portrait" | "landscape"}

Правила:
- prompt — по-английски, 20–80 слов: кто или что изображено, детали, окружение, стиль \
(например, watercolor illustration, anime, 3D render, photorealistic photo), свет и настроение. \
Сохрани всё, о чём просил пользователь, и ничего не добавляй вопреки просьбе. Если стиль \
не назван, выбери подходящий сам.
- Если просьба ссылается на диалог («нарисуй его», «а теперь зимой», «то же, но в стиле аниме»), \
возьми подробности из недавних сообщений и из промпта прошлой картинки.
- Если на картинке просят надпись, дай её в prompt в кавычках, как есть.
- title — короткая подпись по-русски, 2–8 слов: что нарисовано.
- orientation: portrait — человек в полный рост, обои на телефон, постер; landscape — пейзаж, \
панорама, баннер, обложка; в остальных случаях square.
{decision_rule}
Никакого текста вне JSON.\
"""
CLASSIFY_RULE = (
    "- Если сообщение не просьба создать картинку (спрашивают, как научиться рисовать; просят "
    "таблицу, схему или ASCII-рисунок текстом; «нарисуй» в переносном смысле, например "
    "«нарисуй мне план»), ответь {\"draw\": false}."
)
EXPLICIT_RULE = "- Пользователь прямо попросил картинку командой, поэтому draw всегда true."


@dataclass(frozen=True, slots=True)
class DrawPlan:
    request: str  # просьба пользователя как есть (для истории)
    prompt: str  # описание для нейросети-рисовальщика
    title: str  # короткая подпись по-русски
    orientation: str = "square"
    edit: bool = False  # правка картинки, а не новый рисунок


@dataclass(frozen=True, slots=True)
class Drawing:
    id: int  # для кнопки «Ещё вариант»
    plan: DrawPlan
    image: GeneratedImage

    @property
    def caption(self) -> str:
        return f"{'✏️' if self.plan.edit else '🎨'} {self.plan.title}"


def history_text(plan: DrawPlan) -> str:
    """Что сохраняется в истории вместо картинки: Эля помнит, что и как нарисовала."""
    if plan.edit:
        return f"✏️ Изменила картинку: {plan.title}\n\n_Правка: {plan.prompt}_"
    return f"🎨 Нарисовала картинку: {plan.title}\n\n_Промпт: {plan.prompt}_"


_VERB = re.compile(
    r"\b(?:нарису\w*|нарисовать|изобрази\w*|изобразить|сгенери\w*|создай\w*|сделай\w*|"
    r"покажи\w*|придумай\w*|скинь\w*|пришли\w*|draw|generate)\b",
    re.IGNORECASE,
)
_FILLER = re.compile(r"^(?:[\s,.:;!?—-]|\b(?:пожалуйста|плиз|мне|нам|ка)\b)*", re.IGNORECASE)


def strip_intent(text: str) -> str:
    """«Эля, нарисуй, пожалуйста, кота» → «кота»: описание без слов-просьб (для запасного промпта).
    Слово «картинку» и подобные остаются: «сделай аватарку в стиле аниме» → «аватарку в стиле аниме»."""
    clean = " ".join(text.split())
    match = INTENT.search(clean)
    if match is None:
        return clean
    verbs = list(_VERB.finditer(clean, match.start()))
    verbs = [v for v in verbs if v.start() < match.end()]
    rest = clean[verbs[-1].end() if verbs else match.end():]
    rest = _FILLER.sub("", rest).strip(" .,;:!?—-")
    return rest or clean


def plan_from_model(raw: str, request: str, *, explicit: bool) -> DrawPlan | None:
    """None — модель решила, что это не просьба о картинке."""
    try:
        data = extract_json_object(raw)
    except ModelOutputError:
        return fallback_plan(request)
    if data.get("draw") is False and not explicit:
        return None
    prompt = as_str(data.get("prompt"))
    if len(prompt) < 3:
        return fallback_plan(request)
    title = as_str(data.get("title")).strip(" .«»\"") or shorten(strip_intent(request), MAX_TITLE)
    orientation = data.get("orientation") if data.get("orientation") in ORIENTATIONS else "square"
    return DrawPlan(
        request=request,
        prompt=shorten(prompt, MAX_PROMPT),
        title=shorten(title, MAX_TITLE),
        orientation=orientation,
    )


def edit_plan_from_model(raw: str, request: str, *, explicit: bool) -> DrawPlan | None:
    """None — модель решила, что это не просьба изменить картинку."""
    try:
        data = extract_json_object(raw)
    except ModelOutputError:
        return fallback_edit_plan(request) if explicit else None
    if data.get("edit") is False and not explicit:
        return None
    prompt = as_str(data.get("prompt"))
    if len(prompt) < 3:
        return fallback_edit_plan(request)
    title = as_str(data.get("title")).strip(" .«»\"") or shorten(request, MAX_TITLE)
    return DrawPlan(
        request=request, prompt=shorten(prompt, MAX_PROMPT), title=shorten(title, MAX_TITLE), edit=True
    )


def fallback_edit_plan(request: str) -> DrawPlan:
    """Модель недоступна — правим по словам пользователя (FLUX.2 понимает и по-русски)."""
    text = " ".join(request.split())
    return DrawPlan(request=request, prompt=shorten(text, MAX_PROMPT), title=shorten(text, MAX_TITLE), edit=True)


def fallback_plan(request: str) -> DrawPlan:
    """Если модель недоступна или ответила невнятно — рисуем по словам пользователя."""
    description = strip_intent(request)
    return DrawPlan(
        request=request,
        prompt=shorten(description, MAX_PROMPT),
        title=shorten(description, MAX_TITLE),
    )


class DrawingService:
    def __init__(
        self,
        generator: ImageGenerator,
        ai_client: AIClient | None = None,
        memory: MemoryManager | None = None,
        *,
        assistant_name: str = "Эля",
        remember: int = REMEMBER_DRAWINGS,
        clock=time.monotonic,
    ) -> None:
        self._generator = generator
        self._ai = ai_client
        self._memory = memory
        self._assistant_name = assistant_name
        self._remember = remember
        self._clock = clock
        # id картинки -> (пользователь, план, исходник правки, готовая картинка)
        self._recent: OrderedDict[int, tuple[int, DrawPlan, SourceImage | None, bytes]] = OrderedDict()
        self._next_id = 0
        # Последняя картинка пользователя (присланная или готовая) — её правит «измени её».
        self._last: dict[int, tuple[float, bytes]] = {}
        # Картинки одного пользователя рисуются по очереди: сервисам так легче, а история
        # сохраняется в правильном порядке.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def enabled(self) -> bool:
        return self._generator.enabled

    @property
    def generator(self) -> ImageGenerator:
        return self._generator

    @property
    def can_edit(self) -> bool:
        return self._generator.can_edit

    @staticmethod
    def looks_like_request(text: str) -> bool:
        return bool(text) and INTENT.search(text) is not None

    @staticmethod
    def looks_like_edit(text: str) -> bool:
        return bool(text) and EDIT_INTENT.search(text) is not None

    def remember_image(self, user_id: int, data: bytes) -> None:
        """Запомнить картинку как «последнюю»: просьба «измени её» будет о ней."""
        if data:
            self._last[user_id] = (self._clock(), data)

    def last_image(self, user_id: int) -> bytes | None:
        entry = self._last.get(user_id)
        if entry is None or self._clock() - entry[0] > LAST_IMAGE_TTL:
            return None
        return entry[1]

    def select(self, user_id: int, drawing_id: int) -> bool:
        """Кнопка «Изменить» под картинкой: дальше правится именно она."""
        entry = self._recent.get(drawing_id)
        if entry is None or entry[0] != user_id:
            return False
        self.remember_image(user_id, entry[3])
        return True

    async def _context(self, user_id: int) -> str:
        if self._memory is None:
            return ""
        history = (await self._memory.get_history(user_id))[-CONTEXT_MESSAGES:]
        lines = []
        for message in history:
            who = self._assistant_name if message.role == "assistant" else "Пользователь"
            lines.append(f"{who}: {shorten(' '.join(str(message.content).split()), CONTEXT_CHARS)}")
        return "\n".join(lines)

    async def plan(self, user_id: int, text: str, *, explicit: bool = False) -> DrawPlan | None:
        """Промпт для картинки; None — это не просьба нарисовать (только если explicit=False).

        Если языковая модель недоступна, рисуем по словам пользователя: просьба уже
        распознана, а картинки рисует отдельный сервис со своими лимитами.
        """
        request = " ".join(text.split())[:MAX_REQUEST]
        if self._ai is None:
            return fallback_plan(request)
        context = await self._context(user_id)
        user_block = (f"Недавний диалог:\n{context}\n\n" if context else "") + f"Просьба: {request}"
        messages = [
            ChatMessage(
                role="system",
                content=PROMPT_WRITER.replace(
                    "{decision_rule}", EXPLICIT_RULE if explicit else CLASSIFY_RULE
                ),
            ),
            ChatMessage(role="user", content=user_block),
        ]
        try:
            result = await self._ai.complete(messages, temperature=0.4, max_tokens=500)
        except AIError as exc:
            logger.warning("Image prompt writer failed, drawing from the request as is: %s", exc)
            return fallback_plan(request)
        return plan_from_model(result.text, request, explicit=explicit)

    async def plan_edit(self, user_id: int, text: str, *, explicit: bool = False) -> DrawPlan | None:
        """Инструкция для правки; None — это не просьба изменить картинку (если explicit=False)."""
        request = " ".join(text.split())[:MAX_REQUEST]
        if self._ai is None:
            return fallback_edit_plan(request) if explicit or self.looks_like_edit(request) else None
        context = await self._context(user_id)
        user_block = (f"Недавний диалог:\n{context}\n\n" if context else "") + f"Сообщение: {request}"
        messages = [
            ChatMessage(
                role="system",
                content=EDIT_WRITER.replace(
                    "{decision_rule}", EDIT_EXPLICIT_RULE if explicit else EDIT_CLASSIFY_RULE
                ),
            ),
            ChatMessage(role="user", content=user_block),
        ]
        try:
            result = await self._ai.complete(messages, temperature=0.3, max_tokens=400)
        except AIError as exc:
            logger.warning("Image edit prompt writer failed, editing by the request as is: %s", exc)
            return fallback_edit_plan(request)
        return edit_plan_from_model(result.text, request, explicit=explicit)

    async def edit(self, user_id: int, plan: DrawPlan, image: bytes | SourceImage, *, request: str | None = None) -> Drawing:
        """Правит картинку по плану и сохраняет обмен в историю. SourceImageError — файл
        не картинка; ImageGenerationError и ImageLimitError пробрасываются."""
        source = image if isinstance(image, SourceImage) else await asyncio.to_thread(prepare_source, image)
        async with self._locks[user_id]:
            result = await self._generator.edit(plan.prompt, source)
            if self._memory is not None:
                await self._memory.save_exchange(user_id, request or plan.request, history_text(plan))
        self.remember_image(user_id, result.data)
        return Drawing(id=self._keep(user_id, plan, result.data, source), plan=plan, image=result)

    def _keep(self, user_id: int, plan: DrawPlan, result: bytes, source: SourceImage | None = None) -> int:
        self._next_id += 1
        self._recent[self._next_id] = (user_id, plan, source, result)
        while len(self._recent) > self._remember:
            self._recent.popitem(last=False)
        return self._next_id

    async def draw(self, user_id: int, plan: DrawPlan, *, request: str | None = None) -> Drawing:
        """Рисует по плану и сохраняет обмен в историю (request — что записать от имени
        пользователя, по умолчанию его просьба). Ошибки (ImageGenerationError,
        ImageLimitError) пробрасываются, история при этом не меняется."""
        async with self._locks[user_id]:
            image = await self._generator.generate(plan.prompt, orientation=plan.orientation)
            if self._memory is not None:
                await self._memory.save_exchange(user_id, request or plan.request, history_text(plan))
        self.remember_image(user_id, image.data)
        return Drawing(id=self._keep(user_id, plan, image.data), plan=plan, image=image)

    async def again(self, user_id: int, drawing_id: int) -> Drawing | None:
        """Ещё один вариант той же картинки; None — её уже не помню (например, после перезапуска)."""
        entry = self._recent.get(drawing_id)
        if entry is None or entry[0] != user_id:
            return None
        if entry[1].edit and entry[2] is not None:
            return await self.edit(user_id, entry[1], entry[2], request=AGAIN_TEXT)
        return await self.draw(user_id, entry[1], request=AGAIN_TEXT)
