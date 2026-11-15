"""AI-провайдеры и фабрика для их создания из конфигурации."""

from __future__ import annotations

import ssl

import certifi
import httpx

from ai.providers.base import BaseProvider
from ai.providers.gigachat import GigaChatProvider
from ai.providers.openai_compatible import OpenAICompatibleProvider
from config.settings import ProviderConfig


def _client_with_ca(shared: httpx.AsyncClient, ca_bundle: str) -> httpx.AsyncClient:
    """Клиент с теми же настройками, но доверяющий ещё и указанному корневому сертификату."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=ca_bundle)
    return httpx.AsyncClient(verify=context, timeout=shared.timeout, headers=shared.headers)


def build_providers(
    configs: tuple[ProviderConfig, ...], http_client: httpx.AsyncClient
) -> list[BaseProvider]:
    """Порядок провайдеров = порядок фолбэка."""
    providers: list[BaseProvider] = []
    for cfg in configs:
        client = _client_with_ca(http_client, cfg.ca_bundle) if cfg.ca_bundle else http_client
        common = {
            "name": cfg.name,
            "title": cfg.title,
            "base_url": cfg.base_url,
            "models": cfg.models,
            "vision_models": cfg.vision_models,
            "max_output_tokens": cfg.max_output_tokens,
            "http_client": client,
            "owns_client": client is not http_client,
        }
        if cfg.kind == "gigachat":
            providers.append(
                GigaChatProvider(
                    auth_url=cfg.auth_url, auth_key=cfg.api_key, scope=cfg.scope, **common
                )
            )
        else:
            providers.append(OpenAICompatibleProvider(api_key=cfg.api_key, **common))
    return providers
