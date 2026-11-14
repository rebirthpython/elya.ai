"""Анимации и переходы слайдов в формате PowerPoint (python-pptx их не поддерживает).

Анимация описывается шагами: каждый шаг запускается щелчком (первый может стартовать
сам после появления слайда), внутри шага эффекты идут одновременно. Эффект — появление
фигуры целиком или одного её абзаца (пункта списка).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from lxml import etree
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.slide import Slide

_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NSDECL = f'xmlns:p="{_P_NS}" xmlns:a="{_A_NS}"'

# Эффект -> (presetID, presetSubtype, длительность, мс). Номера пресетов — как у PowerPoint,
# чтобы эффекты правильно назывались в панели анимации.
_PRESETS: dict[str, tuple[int, int, int]] = {
    "fade": (10, 0, 500),
    "wipe": (22, 8, 500),
    "rise": (42, 0, 600),
    "zoom": (53, 16, 500),
    "fly_left": (2, 8, 600),
    "fly_right": (2, 2, 600),
    "fly_up": (2, 4, 600),
}
EFFECTS = tuple(_PRESETS)

_TRANSITIONS = {
    "fade": "<p:fade/>",
    "push": '<p:push dir="u"/>',
    "wipe": '<p:wipe dir="r"/>',
}


@dataclass(frozen=True)
class Effect:
    shape_id: int
    kind: str = "fade"
    paragraph: int | None = None  # None — фигура целиком


def _target(effect: Effect) -> str:
    text = (
        f'<p:txEl><p:pRg st="{effect.paragraph}" end="{effect.paragraph}"/></p:txEl>'
        if effect.paragraph is not None
        else ""
    )
    return f'<p:tgtEl><p:spTgt spid="{effect.shape_id}">{text}</p:spTgt></p:tgtEl>'


def _anim(ids: count, target: str, attr: str, start: str, end: str, duration: int) -> str:
    return (
        '<p:anim calcmode="lin" valueType="num"><p:cBhvr additive="base">'
        f'<p:cTn id="{next(ids)}" dur="{duration}" fill="hold"/>{target}'
        f"<p:attrNameLst><p:attrName>{attr}</p:attrName></p:attrNameLst></p:cBhvr>"
        f'<p:tavLst><p:tav tm="0"><p:val><p:strVal val="{start}"/></p:val></p:tav>'
        f'<p:tav tm="100000"><p:val><p:strVal val="{end}"/></p:val></p:tav></p:tavLst></p:anim>'
    )


def _fade(ids: count, target: str, duration: int, kind: str = "fade") -> str:
    return (
        f'<p:animEffect transition="in" filter="{kind}"><p:cBhvr>'
        f'<p:cTn id="{next(ids)}" dur="{duration}"/>{target}</p:cBhvr></p:animEffect>'
    )


def _behaviors(ids: count, effect: Effect, duration: int) -> str:
    target = _target(effect)
    show = (
        f'<p:set><p:cBhvr><p:cTn id="{next(ids)}" dur="1" fill="hold">'
        '<p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
        f"{target}<p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>"
        '</p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>'
    )
    kind = effect.kind
    if kind == "fade":
        return show + _fade(ids, target, duration)
    if kind == "wipe":
        return show + _fade(ids, target, duration, "wipe(left)")
    if kind == "rise":
        return (
            show
            + _fade(ids, target, duration)
            + _anim(ids, target, "ppt_y", "#ppt_y+.1", "#ppt_y", duration)
        )
    if kind == "zoom":
        return (
            show
            + _fade(ids, target, duration)
            + _anim(ids, target, "ppt_w", "0", "#ppt_w", duration)
            + _anim(ids, target, "ppt_h", "0", "#ppt_h", duration)
        )
    start_x, start_y = {
        "fly_left": ("0-#ppt_w/2", "#ppt_y"),
        "fly_right": ("1+#ppt_w/2", "#ppt_y"),
        "fly_up": ("#ppt_x", "1+#ppt_h/2"),
    }[kind]
    return (
        show
        + _anim(ids, target, "ppt_x", start_x, "#ppt_x", duration)
        + _anim(ids, target, "ppt_y", start_y, "#ppt_y", duration)
    )


def _effect_node(ids: count, effect: Effect, node_type: str) -> str:
    preset, subtype, duration = _PRESETS[effect.kind]
    return (
        f'<p:par><p:cTn id="{next(ids)}" presetID="{preset}" presetClass="entr" '
        f'presetSubtype="{subtype}" fill="hold" grpId="0" nodeType="{node_type}">'
        '<p:stCondLst><p:cond delay="0"/></p:stCondLst>'
        f"<p:childTnLst>{_behaviors(ids, effect, duration)}</p:childTnLst></p:cTn></p:par>"
    )


def _step(ids: count, effects: list[Effect], *, auto: bool) -> str:
    outer_id, inner_id = next(ids), next(ids)
    start = (
        '<p:cond delay="indefinite"/><p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond>'
        if auto
        else '<p:cond delay="indefinite"/>'
    )
    first = "afterEffect" if auto else "clickEffect"
    nodes = "".join(
        _effect_node(ids, effect, first if i == 0 else "withEffect")
        for i, effect in enumerate(effects)
    )
    return (
        f'<p:par><p:cTn id="{outer_id}" fill="hold"><p:stCondLst>{start}</p:stCondLst><p:childTnLst>'
        f'<p:par><p:cTn id="{inner_id}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst>'
        f"<p:childTnLst>{nodes}</p:childTnLst></p:cTn></p:par>"
        "</p:childTnLst></p:cTn></p:par>"
    )


def _build_list(steps: list[list[Effect]]) -> str:
    by_paragraph: dict[int, bool] = {}
    for effect in (e for step in steps for e in step):
        by_paragraph[effect.shape_id] = by_paragraph.get(effect.shape_id, False) or (
            effect.paragraph is not None
        )
    return "".join(
        f'<p:bldP spid="{spid}" grpId="0" build="p"/>' if paragraphs
        else f'<p:bldP spid="{spid}" grpId="0" animBg="1"/>'
        for spid, paragraphs in by_paragraph.items()
    )


def timing_xml(steps: list[list[Effect]], *, auto_first: bool = False) -> str:
    for effect in (e for step in steps for e in step):
        if effect.kind not in _PRESETS:
            raise ValueError(f"unknown effect: {effect.kind}")
    ids = count(3)  # 1 — корень, 2 — главная последовательность
    body = "".join(
        _step(ids, step, auto=auto_first and i == 0) for i, step in enumerate(steps) if step
    )
    return (
        f"<p:timing {_NSDECL}><p:tnLst><p:par>"
        '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
        '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq">'
        f"<p:childTnLst>{body}</p:childTnLst></p:cTn>"
        '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
        '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
        "</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>"
        f"<p:bldLst>{_build_list(steps)}</p:bldLst></p:timing>"
    )


def _insert_before_ext(slide: Slide, element: etree._Element) -> None:
    """Порядок в p:sld по схеме: cSld, clrMapOvr, transition, timing, extLst."""
    sld = slide._element
    ext = sld.find(qn("p:extLst"))
    if ext is not None:
        ext.addprevious(element)
    else:
        sld.append(element)


def add_transition(slide: Slide, kind: str, speed: str = "med") -> None:
    if kind not in _TRANSITIONS:
        raise ValueError(f"unknown transition: {kind}")
    element = parse_xml(f'<p:transition {_NSDECL} spd="{speed}">{_TRANSITIONS[kind]}</p:transition>')
    timing = slide._element.find(qn("p:timing"))
    if timing is not None:
        timing.addprevious(element)
    else:
        _insert_before_ext(slide, element)


def add_animations(slide: Slide, steps: list[list[Effect]], *, auto_first: bool = False) -> None:
    steps = [step for step in steps if step]
    if steps:
        _insert_before_ext(slide, parse_xml(timing_xml(steps, auto_first=auto_first)))
