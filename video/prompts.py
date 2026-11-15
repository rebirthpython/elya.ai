"""Промпт монтажёра: просьба человека -> план монтажа в JSON."""

from __future__ import annotations

from services.speech import TimedTranscript
from study.prompts import JSON_ONLY
from video.ffmpeg import ClipInfo
from video.plan import EditPlan, fmt_time

PLANNER_PROMPT = """\
Ты — {name}, монтажёр видео в Telegram. Человек прислал клипы и просит их смонтировать. \
Преврати просьбу в план монтажа. Монтирует код строго по плану; само видео ты не видишь — \
только длительность и размер клипов и, если есть, расшифровку речи.

Ответ — JSON-объект с полями (все поля плана, даже неизменённые):
{{
  "timeline": [{{"clip": 1, "from": "0:00", "to": "0:15", "speed": 1}}],
  "remove_pauses": false,
  "subtitles": false,
  "texts": [{{"text": "Лето 2026", "from": 0, "to": 3, "position": "center"}}],
  "format": "original",
  "fit": "blur",
  "rotate": 0,
  "mirror": false,
  "stabilize": false,
  "filter": "none",
  "transitions": false,
  "fade": false,
  "volume": 1,
  "clean_audio": false,
  "music": false,
  "music_volume": 0.3,
  "output": "video",
  "need_transcript": false,
  "note": ""
}}

Что означают поля:
- timeline — фрагменты итогового видео по порядку. clip — номер клипа (с 1), from и to — время \
в ИСХОДНОМ клипе секундами или «м:сс»; "to": null — до конца клипа. Клипы можно переставлять, \
повторять и пропускать. speed — скорость фрагмента от 0.25 до 4 (замедлить вдвое — 0.5).
- remove_pauses — вырезать паузы и тишину между фразами (для видео, где говорят).
- subtitles — субтитры из речи; текст распознаётся автоматически, писать его не нужно.
- texts — надписи поверх видео, до 80 символов. from и to — время в ИТОГОВОМ видео, \
"to": null — до конца. position: top | center | bottom.
- format: original | 9:16 (вертикально: сторис, Reels, TikTok, Shorts) | 16:9 (YouTube) | \
1:1 (квадрат) | 4:5 (лента Instagram).
- fit — как вписать в новый формат: blur (целиком, по краям размытый фон), crop (на весь экран, \
края обрезаны), pad (чёрные полосы).
- rotate — поворот по часовой стрелке: 0 | 90 | 180 | 270. mirror — отразить слева направо.
- stabilize — убрать тряску.
- filter: none | bw (чёрно-белый) | warm (тёплый) | cool (холодный) | vivid (сочные цвета) | \
vintage | bright (светлее).
- transitions — плавные затемнения на стыках фрагментов. fade — плавное начало и конец видео.
- volume — громкость звука видео: 1 — как есть, 0 — без звука, до 3 — громче.
- clean_audio — убрать шум и выровнять громкость голоса.
- music — наложить присланную музыку (только если она прислана). music_volume — её громкость \
от 0.05 до 1; фоном под голос — 0.2–0.3, без голоса — 0.8–1.
- output: video | gif (гифка без звука) | circle (кружок Telegram, до 60 с) | audio (только звук).
- need_transcript — true, если нужно знать, что говорят в видео (найти момент по смыслу, \
вырезать оговорки, оставить самое интересное), а расшифровки ниже нет. Остальное тогда не меняй.
- note — коротко и по-доброму, если что-то из просьбы сделать нельзя (спецэффекты, удалить \
предмет из кадра, найти момент по картинке), иначе "". Если сообщение вообще не про монтаж, \
оставь план без изменений и ответь в note одной фразой.

Правила:
- Начни с текущего плана и поменяй только то, о чём просят. Каждая новая просьба — правка \
текущего результата, а не монтаж заново, если человек не просит начать сначала.
- «Обрежь первые 5 секунд» → from 5. «Оставь последние 10 секунд» клипа длиной 0:40 → from 30. \
«Вырежи с 0:10 до 0:15» → два фрагмента: до 0:10 и от 0:15. «Склей» → все клипы по порядку.
- Время не выходит за длительность клипа. Если человек называет время, которого нет, возьми \
ближайшее допустимое.
- Ничего не добавляй от себя: ни надписей, ни музыки, ни фильтров, если о них не просили.
{json_only}
"""


def planner_prompt(assistant_name: str) -> str:
    return PLANNER_PROMPT.format(name=assistant_name, json_only=JSON_ONLY)


def _clip_line(number: int, clip: ClipInfo) -> str:
    sound = "со звуком" if clip.has_audio else "без звука"
    return (f"{number}) длительность {fmt_time(clip.duration)} ({clip.duration:.1f} с), "
            f"{clip.width}×{clip.height}, {clip.orientation}, {sound}")


def transcript_block(transcripts: dict[int, TimedTranscript], limit: int = 8000) -> str:
    lines = ["Расшифровка речи (время — в исходных клипах):"]
    for number, transcript in sorted(transcripts.items()):
        lines.append(f"Клип {number}:")
        if not transcript.segments:
            lines.append("  (речи нет)")
        for seg in transcript.segments:
            lines.append(f"  [{fmt_time(seg.start)}–{fmt_time(seg.end)}] {seg.text}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n  …(дальше обрезано)"


def planner_request(
    request: str,
    clips: list[ClipInfo],
    current: EditPlan,
    *,
    has_music: bool,
    music_duration: float | None = None,
    can_subtitles: bool,
    history: list[str] = (),
    transcripts: dict[int, TimedTranscript] | None = None,
) -> str:
    parts = ["Клипы:", *(_clip_line(i + 1, c) for i, c in enumerate(clips))]
    if has_music:
        length = f" ({fmt_time(music_duration)})" if music_duration else ""
        parts.append(f"Музыка: прислана{length}.")
    else:
        parts.append("Музыка: не прислана.")
    parts.append("Субтитры: можно." if can_subtitles else "Субтитры: недоступны.")
    parts.append(f"\nТекущий план (JSON):\n{current.to_json()}")
    if transcripts:
        parts.append("\n" + transcript_block(transcripts))
    if history:
        parts.append("\nПрошлые просьбы (уже выполнены):\n" + "\n".join(f"- {h}" for h in history[-5:]))
    parts.append(f"\nПросьба:\n<<<\n{request}\n>>>")
    return "\n".join(parts)
