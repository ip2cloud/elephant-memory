"""Registro de projetos — a autoridade unica sobre o que existe.

Antes disto, "projeto existe" era inferido do banco: primeiro por ter no
`:Entity`, depois por ter qualquer no. Os dois eram remendo, e cada um errava
para um lado:

  - por `:Entity`  -> ingestao que nao extrai entidade some da lista
  - por qualquer no -> grafo criado por acidente vira projeto

O registro resolve os dois de uma vez. Projeto e o que esta declarado aqui:
registrado e vazio continua visivel; grafo orfao nunca vira projeto.

Declarativo de proposito. Criar projeto e ato administrativo — editar a
configuracao e reiniciar. Nenhuma credencial cria projeto escrevendo.
"""

from __future__ import annotations

import json
import os
import re

from .config import env

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def load_registry() -> frozenset[str]:
    """PROJECTS aceita JSON (`["a","b"]`) ou lista separada por virgula."""
    raw = (env("PROJECTS") or "").strip()
    if not raw:
        return frozenset()

    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"PROJECTS nao e JSON valido: {exc}") from exc
    else:
        items = [p for p in re.split(r"[,\s]+", raw) if p]

    projetos: set[str] = set()
    invalidos: list[str] = []
    for item in items:
        value = str(item).strip().lower()
        if _ID_RE.match(value):
            projetos.add(value)
        else:
            invalidos.append(value)

    if invalidos:
        raise RuntimeError(
            f"IDs de projeto invalidos em PROJECTS: {sorted(invalidos)}. "
            "Use [a-z0-9._-], comecando com letra ou digito, ate 64 chars."
        )
    return frozenset(projetos)


def assert_grants_within_registry(registry: frozenset[str], grants_por_credencial) -> None:
    """Recusa subir se alguma credencial apontar para projeto fora do registro.

    Typo em permissao vira erro de inicializacao, nao descoberta tardia: sem
    isto, `read:scout-manger` simplesmente nunca casaria com nada e o sintoma
    seria "nao acho nada nesse projeto", semanas depois.
    """
    if not registry:
        return
    orfaos: set[str] = set()
    for grants in grants_por_credencial:
        for grant in grants:
            _, _, projeto = grant.partition(":")
            if projeto and projeto not in registry:
                orfaos.add(grant)
    if orfaos:
        raise RuntimeError(
            f"Grants apontam para projeto fora de PROJECTS: {sorted(orfaos)}. "
            f"Registrados: {sorted(registry)}."
        )
