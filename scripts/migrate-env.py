#!/usr/bin/env python3
"""Migra o .env para o formato de permissao por projeto.

Existe porque escrever `.env` por ferramenta remota e bloqueado — e porque
editar JSON com token na mao e como se pede erro.

    python3 scripts/migrate-env.py                       # projetos padrao
    python3 scripts/migrate-env.py scout-manager grafity # explicito

Faz duas coisas, preservando os tokens que ja estao la:

  1. acrescenta PROJECTS, se faltar
  2. converte `scopes` de acao global para acao:projeto
       ["read","write"]  ->  ["read:scout-manager","write:scout-manager", ...]
     e da grants de leitura para entradas na forma curta (token -> "nome"),
     que no formato novo concedem ZERO acesso.

Idempotente: rodar duas vezes nao muda nada. Grava backup em .env.bak.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ACOES = ("read", "write", "ingest")


def main() -> int:
    projetos = sys.argv[1:] or ["scout-manager", "grafity"]

    env = Path(".env")
    if not env.is_file():
        print("migrate-env: .env nao encontrado. Rode na raiz do projeto.", file=sys.stderr)
        return 1

    texto = env.read_text(encoding="utf-8")
    original = texto

    # 1) PROJECTS
    if not re.search(r"^PROJECTS=", texto, re.M):
        bloco = (
            "# Registro de projetos — a AUTORIDADE sobre o que existe. ID fora daqui\n"
            "# falha, sem criar grafo. Criar projeto e ato administrativo: editar\n"
            "# aqui e reiniciar. Nenhuma credencial cria projeto escrevendo.\n"
            f"PROJECTS={json.dumps(projetos, separators=(',', ':'))}\n\n"
        )
        if re.search(r"^AUTH_MODE=", texto, re.M):
            texto = re.sub(r"^(AUTH_MODE=)", bloco + r"\1", texto, count=1, flags=re.M)
        else:
            texto = bloco + texto

    # 2) AUTH_TOKENS
    m = re.search(r"^AUTH_TOKENS=(['\"]?)(\{.*\})\1\s*$", texto, re.M)
    if m:
        try:
            data = json.loads(m.group(2))
        except json.JSONDecodeError as exc:
            print(f"migrate-env: AUTH_TOKENS nao e JSON valido ({exc}). Nada alterado.",
                  file=sys.stderr)
            return 1

        novo = {}
        for token, value in data.items():
            if isinstance(value, str):
                # Forma curta: no formato novo concede zero. Da leitura.
                novo[token] = {"user": value,
                               "scopes": [f"read:{p}" for p in projetos]}
                continue
            escopos = value.get("scopes") or []
            if any(":" in str(s) for s in escopos):
                novo[token] = value          # ja migrado
                continue
            acoes = [a for a in escopos if a in ACOES]
            novo[token] = {"user": value.get("user"),
                           "scopes": [f"{a}:{p}" for a in acoes for p in projetos]}

        texto = (texto[:m.start()]
                 + "AUTH_TOKENS='" + json.dumps(novo, separators=(",", ":")) + "'"
                 + texto[m.end():])

    if texto == original:
        print("migrate-env: nada a fazer, ja esta no formato novo.")
        return 0

    shutil.copy(env, ".env.bak")
    env.write_text(texto, encoding="utf-8")
    print(f"migrate-env: .env atualizado (backup em .env.bak). Projetos: {projetos}")
    print("Confira com:  grep -E '^(PROJECTS|AUTH_MODE)=' .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
