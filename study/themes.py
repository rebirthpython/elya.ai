"""Темы оформления презентаций. Шрифты — стандартные, с кириллицей, есть в Windows и macOS."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    key: str
    title: str
    background: str
    text: str
    muted: str
    accent: str
    accent2: str
    card: str
    # Титульный слайд, разделители и финальный слайд
    cover_background: str
    cover_text: str
    cover_muted: str
    cover_accent: str
    heading_font: str
    body_font: str
    transition: str  # fade | push | wipe
    bullet_effect: str  # fade | rise | wipe


THEMES: dict[str, Theme] = {
    theme.key: theme
    for theme in (
        Theme(
            key="classic",
            title="Строгая",
            background="FFFFFF",
            text="1F2937",
            muted="6B7280",
            accent="2563EB",
            accent2="1E3A5F",
            card="EEF3FA",
            cover_background="1E3A5F",
            cover_text="FFFFFF",
            cover_muted="BFD3EE",
            cover_accent="60A5FA",
            heading_font="Calibri",
            body_font="Calibri",
            transition="fade",
            bullet_effect="fade",
        ),
        Theme(
            key="bright",
            title="Яркая",
            background="FFF8F1",
            text="2D2A32",
            muted="7A6F7D",
            accent="F25C3B",
            accent2="FFB703",
            card="FFFFFF",
            cover_background="F25C3B",
            cover_text="FFFFFF",
            cover_muted="FFE3D6",
            cover_accent="FFD166",
            heading_font="Trebuchet MS",
            body_font="Trebuchet MS",
            transition="push",
            bullet_effect="rise",
        ),
        Theme(
            key="minimal",
            title="Минимализм",
            background="FAFAF7",
            text="141414",
            muted="7C7C7C",
            accent="141414",
            accent2="B08D57",
            card="F0EEE8",
            cover_background="FAFAF7",
            cover_text="141414",
            cover_muted="7C7C7C",
            cover_accent="B08D57",
            heading_font="Georgia",
            body_font="Arial",
            transition="fade",
            bullet_effect="fade",
        ),
        Theme(
            key="dark",
            title="Тёмная",
            background="0F172A",
            text="F1F5F9",
            muted="94A3B8",
            accent="38BDF8",
            accent2="F59E0B",
            card="1E293B",
            cover_background="020617",
            cover_text="F8FAFC",
            cover_muted="94A3B8",
            cover_accent="F59E0B",
            heading_font="Arial",
            body_font="Arial",
            transition="wipe",
            bullet_effect="rise",
        ),
    )
}
DEFAULT_THEME = "classic"
