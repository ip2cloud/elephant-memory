"""Camada fina sobre o graphiti_core, com o escopo de projeto aplicado a forca.

Mapeamento:

    identidade do Cloudflare Access  ->  quem esta chamando (log/auditoria)
    X-Project-Id (ou /p/<projeto>)   ->  group_id do Graphiti

`group_id` e a particao nativa do grafo no Graphiti: `add_episode(group_id=...)`
e `search(group_ids=[...])`. Nao ha campo separado de usuario — se um dia voce
compartilhar o servidor com outra pessoa, prefixe o group_id com o dono
(`alfredo:acme-api-billing`) em vez de inventar um campo.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver

from .config import env

logger = logging.getLogger("graphmem.store")

_client: Graphiti | None = None
_lock = asyncio.Lock()


def build_driver() -> FalkorDriver:
    return FalkorDriver(
        host=os.environ.get("FALKORDB_HOST", "falkordb"),
        port=int(os.environ.get("FALKORDB_PORT", "6379")),
        password=env("FALKORDB_PASSWORD") or None,
        database=os.environ.get("FALKORDB_DATABASE", "memoria"),
    )


async def get_client() -> Graphiti:
    """Singleton. A primeira chamada tambem cria indices e constraints."""
    global _client
    if _client is None:
        async with _lock:
            if _client is None:
                client = Graphiti(graph_driver=build_driver())
                await client.build_indices_and_constraints()
                _client = client
                logger.info("graphiti inicializado")
    return _client


async def set_client(client: Graphiti) -> None:
    """Injecao usada pelos testes (driver e stubs proprios)."""
    global _client
    _client = client


def shape_fact(edge: Any) -> dict[str, Any]:
    """Reduz um EntityEdge ao que serve pro modelo.

    Inclui `valid_at` / `invalid_at` de proposito: e a parte bitemporal, o que
    diferencia isto de uma busca vetorial. `invalid_at` preenchido significa
    "isto ja foi verdade e deixou de ser" — informacao que muda a resposta.
    """
    return {
        "fact": getattr(edge, "fact", None),
        "project": getattr(edge, "group_id", None),
        "uuid": str(getattr(edge, "uuid", "")),
        "valid_at": _iso(getattr(edge, "valid_at", None)),
        "invalid_at": _iso(getattr(edge, "invalid_at", None)),
        "created_at": _iso(getattr(edge, "created_at", None)),
    }


def shape_node(node: Any) -> dict[str, Any]:
    return {
        "name": getattr(node, "name", None),
        "summary": getattr(node, "summary", None),
        "project": getattr(node, "group_id", None),
        "uuid": str(getattr(node, "uuid", "")),
        "labels": [l for l in (getattr(node, "labels", None) or []) if l != "Entity"],
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
