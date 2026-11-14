"""Рисование в диалоге: распознать просьбу, написать промпт, нарисовать, запомнить.

Просьбу («нарисуй кота в шляпе», /draw …) языковая модель превращает в подробный промпт
по-английски — так картинки получаются заметно лучше — с учётом недавнего диалога, поэтому
работают и уточнения вроде «а теперь то же самое, но зимой». Рисует отдельный сервис
(services.imagegen). В историю попадает подпись и промпт, сама картинка не хранится.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from ai.client import AIClient, AIError
from ai.providers.base import ChatMessage
from memory.manager import MemoryManager
from services.imagegen import ORIENTATIONS, GeneratedImage, ImageGenerator
from study.jsonutil import ModelOutputError, as_str, extract_json_object, shorten

logger = logging.getLogger(__name__)

MAX_REQUEST = 1000
MAX_PROMPT = 1500
MAX_TITLE = 80
CONTEXT_MESSAGES = 6
CONTEXT_CHARS = 500
REMEMBER_DRAWINGS = 100  # сколько последних картинок можно перерисовать кнопкой «Ещё вариант»
AGAIN_TEXT = "🔄 Ещё вариант"

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


@dataclass(frozen=True, slots=True)
class Drawing:
    id: int  # для кнопки «Ещё вариант»
    plan: DrawPlan
    image: GeneratedImage

    @property
    def caption(self) -> str:
        return f"🎨 {self.plan.title}"


def history_text(plan: DrawPlan) -> str:
    """Что сохраняется в истории вместо картинки: Эля помнит, что и как нарисовала."""
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
    ) -> None:
        self._generator = generator
        self._ai = ai_client
        self._memory = memory
        self._assistant_name = assistant_name
        self._remember = remember
        self._recent: OrderedDict[int, tuple[int, DrawPlan]] = OrderedDict()
        self._next_id = 0
        # Картинки одного пользователя рисуются по очереди: сервисам так легче, а история
        # сохраняется в правильном порядке.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def enabled(self) -> bool:
        return self._generator.enabled

    @property
    def generator(self) -> ImageGenerator:
        return self._generator

    @staticmethod
    def looks_like_request(text: str) -> bool:
        return bool(text) and INTENT.search(text) is not None

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

    def _keep(self, user_id: int, plan: DrawPlan) -> int:
        self._next_id += 1
        self._recent[self._next_id] = (user_id, plan)
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
        return Drawing(id=self._keep(user_id, plan), plan=plan, image=image)

    async def again(self, user_id: int, drawing_id: int) -> Drawing | None:
        """Ещё один вариант той же картинки; None — её уже не помню (например, после перезапуска)."""
        entry = self._recent.get(drawing_id)
        if entry is None or entry[0] != user_id:
            return None
        return await self.draw(user_id, entry[1], request=AGAIN_TEXT)
