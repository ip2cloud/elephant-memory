"""Escopo da requisicao: quem chamou, de qual projeto, e o que pode fazer nele.

Duas regras estruturais:

1. O projeto NAO vem do modelo. Chega no transporte (header `X-Project-Id` ou
   prefixo `/p/<projeto>/`) e e fixado num ContextVar antes de qualquer tool.

2. Permissao e POR PROJETO, nao global: `read:scout-manager`,
   `write:scout-manager`, `ingest:scout-manager`. Nao existe token com escrita
   em tudo. Consequencia util: `_all` deixa de significar "tudo no servidor" e
   passa a significar "tudo que ESTA credencial pode ler".

O escopo reservado ALL_PROJECTS ("_all") atende clientes sem projeto por sessao
(o Cowork): leitura ampla, escrita exigindo destino explicito.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

ALL_PROJECTS = "_all"

READ = "read"
WRITE = "write"    # MCP `remember`
INGEST = "ingest"  # endpoint REST /ingest, usado pelo publicador do handoff

VALID_ACTIONS = frozenset({READ, WRITE, INGEST})

_GRANT_RE = re.compile(r"^(read|write|ingest):([a-z0-9][a-z0-9._-]{0,63})$")


class ScopeError(ValueError):
    """Escopo ausente ou invalido na requisicao."""


class PermissionDenied(PermissionError):
    """Credencial autenticada, mas sem o grant necessario."""


@dataclass(frozen=True)
class Scope:
    user_id: str
    project_id: str
    grants: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_cross_project(self) -> bool:
        return self.project_id == ALL_PROJECTS

    def projects_for(self, action: str) -> set[str]:
        prefix = f"{action}:"
        return {g[len(prefix):] for g in self.grants if g.startswith(prefix)}

    def allows(self, action: str, project: str) -> bool:
        return f"{action}:{project}" in self.grants

    def require(self, action: str, project: str) -> None:
        if not self.allows(action, project):
            concedidas = ", ".join(sorted(self.grants)) or "(nenhuma)"
            raise PermissionDenied(
                f"Esta credencial nao tem '{action}:{project}'. Concedidas: {concedidas}."
            )


_scope: ContextVar[Scope | None] = ContextVar("graph_scope", default=None)


def normalize_project_id(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value == ALL_PROJECTS:
        return ALL_PROJECTS
    if not PROJECT_ID_RE.match(value):
        raise ScopeError(
            f"Projeto invalido: {raw!r}. Use [a-z0-9._-], comecando com letra "
            f"ou digito, ate 64 chars — ou '{ALL_PROJECTS}' para leitura ampla."
        )
    return value


def parse_grants(raw) -> frozenset[str]:
    """Aceita lista ou string separada por espaco/virgula.

    Grant mal formado e ERRO de inicializacao, nao aviso. Um typo em
    `wirte:scout-manager` viraria silenciosamente uma credencial sem escrita, e
    o sintoma apareceria semanas depois, parecendo problema de autenticacao.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]

    grants: set[str] = set()
    invalidos: list[str] = []
    for item in raw:
        value = str(item).strip().lower()
        if not value:
            continue
        if _GRANT_RE.match(value):
            grants.add(value)
        else:
            invalidos.append(value)

    if invalidos:
        raise RuntimeError(
            f"Grants invalidos: {sorted(invalidos)}. "
            f"Formato: <{'|'.join(sorted(VALID_ACTIONS))}>:<projeto>, "
            "por exemplo 'read:scout-manager'. Permissao global nao existe."
        )
    return frozenset(grants)


def set_scope(user_id: str, project_id: str, grants: frozenset[str] | None = None):
    return _scope.set(
        Scope(user_id=user_id, project_id=project_id, grants=grants or frozenset())
    )


def reset_scope(token) -> None:
    _scope.reset(token)


def current_scope() -> Scope:
    scope = _scope.get()
    if scope is None:
        raise ScopeError(
            "Escopo ausente. Esta tool so pode ser chamada via HTTP com "
            "identidade e projeto resolvidos."
        )
    return scope
