"""AI-провайдеры и фабрика для их создания из конфигурации."""

from __future__ import annotations

import httpx

from ai.providers.base import BaseProvider
from ai.providers.openai_compatible import OpenAICompatibleProvider
from config.settings import ProviderConfig


def build_providers(
    configs: tuple[ProviderConfig, ...], http_client: httpx.AsyncClient
) -> list[BaseProvider]:
    """Порядок провайдеров = порядок фолбэка."""
    return [
        OpenAICompatibleProvider(
            name=cfg.name,
            title=cfg.title,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            models=cfg.models,
            http_client=http_client,
        )
        for cfg in configs
    ]
