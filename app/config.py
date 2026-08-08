"""Leitura de configuracao com suporte a Docker Swarm secrets.

Convencao da infra: segredo nao vai em variavel de ambiente, vai em
`/run/secrets/<nome>` e a variavel aponta para o arquivo:

    OPENAI_API_KEY_FILE: /run/secrets/graphiti_openai_key

Motivo pratico: variavel de ambiente vaza em `docker inspect`, em log de
crash e na UI do Portainer. Arquivo de secret nao.

`env("X")` procura, nesta ordem: `X_FILE` (le o arquivo) e depois `X`.
"""

from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    """Valor de `name`, aceitando `name_FILE` apontando para um secret."""
    caminho = os.environ.get(f"{name}_FILE", "").strip()
    if caminho:
        try:
            # rstrip("\n"): editor e `echo` costumam deixar newline no fim, e um
            # token com \n falha a comparacao sem dar erro que explique nada.
            return Path(caminho).read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            raise RuntimeError(
                f"{name}_FILE aponta para {caminho}, que nao pode ser lido: {exc}. "
                "Em Swarm, confira se o secret esta declarado no servico."
            ) from exc
    return os.environ.get(name, default)


def env_required(name: str) -> str:
    valor = env(name)
    if not valor:
        raise RuntimeError(
            f"{name} nao definido. Em Swarm, use {name}_FILE apontando para "
            f"/run/secrets/<nome>; local, defina {name} no .env."
        )
    return valor


# Variaveis que bibliotecas de terceiros leem direto do ambiente e que,
# portanto, precisam existir como env var mesmo vindo de secret.
_PROMOVER = ("OPENAI_API_KEY",)


def hydrate_env() -> None:
    """Promove `X_FILE` para `X` no ambiente do processo.

    O SDK da OpenAI (usado por dentro do Graphiti) le OPENAI_API_KEY do
    ambiente; ele nao conhece a convencao `_FILE`. Sem esta promocao, o
    segredo em Swarm chega no arquivo e a biblioteca nao acha nada.
    """
    for nome in _PROMOVER:
        if os.environ.get(nome):
            continue
        valor = env(nome)
        if valor:
            os.environ[nome] = valor
