"""Ledger — chave idempotente da fachada E autoridade do que esta vigente.

Dois papeis, e o segundo foi acrescentado depois de constatar que faltava:

1. **Idempotencia.** `add_episode(uuid=X)` do Graphiti BUSCA um episodio
   existente (`EpisodicNode.get_by_uuid`), nao cria um com aquele id. Entao o
   hash da decisao nao pode ser o uuid do episodio. O Graphiti gera o uuid; o
   par (hash -> uuid) mora aqui.

2. **Estado de vigencia.** O `recall` antes nao consultava o ledger, e por isso
   `supersede` e `retract` nao tinham efeito nenhum na leitura — o tombstone
   escrevia o texto no episodio e torcia para o LLM notar a contradicao. Agora
   o ledger e a autoridade: o Graphiti guarda historico e relacoes temporais, o
   ledger diz o que ainda vale.

       active --supersede--> superseded
       active --retract----> retracted

SQLite num volume. E estado: sem volume, zera no restart e tudo e republicado.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Estados de publicacao
PENDING = "pending"
PUBLISHED = "published"
FAILED = "failed"

# Estados de vigencia
ACTIVE = "active"
SUPERSEDED = "superseded"
RETRACTED = "retracted"

INVALID_STATES = frozenset({SUPERSEDED, RETRACTED})

# Versao da canonicalizacao. Mudar a regra SEM subir este numero faz todo o
# corpus existente virar "decisao nova" na proxima publicacao.
CANON_VERSION = 1


def canonical_hash(
    *,
    project_id: str,
    decision_id: str,
    operation: str,
    statement: str | None,
    rationale: str | None,
    authority_refs: list[str] | None,
    target_decision_id: str | None = None,
) -> str:
    """Hash canonico da decisao — base RFC 8785 (JSON canonico).

    Normaliza a FORMA, nunca o CONTEUDO:
      - Unicode em NFC (acento composto vs. pre-composto e a mesma letra)
      - chaves ordenadas, sem espaco insignificante, UTF-8
      - **caixa preservada**

    O casefold da versao anterior era um defeito: fazia `userId` e `USERID`
    serem a mesma decisao. Em decisao de software, nao sao.
    """
    def norm(value):
        if value is None:
            return None
        return unicodedata.normalize("NFC", str(value)).strip()

    payload = {
        "v": CANON_VERSION,
        "project_id": norm(project_id),
        "decision_id": norm(decision_id),
        "operation": norm(operation),
        "target_decision_id": norm(target_decision_id),
        "statement": norm(statement),
        "rationale": norm(rationale),
        # Ordem preservada: a sequencia das fontes e informacao do autor.
        "authority_refs": [norm(r) for r in (authority_refs or [])],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    group_id: str
    decision_id: str
    decision_hash: str
    publication: str
    state: str
    episode_uuid: str | None
    attempts: int
    published_at: str | None
    invalidated_by: str | None


_COLS = (
    "group_id, decision_id, decision_hash, publication, state, "
    "episode_uuid, attempts, published_at, invalidated_by"
)


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                path = os.environ.get("LEDGER_DB", "/data/ledger.db")
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                conn = sqlite3.connect(path, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publications (
                        group_id       TEXT NOT NULL,
                        decision_id    TEXT NOT NULL,
                        decision_hash  TEXT NOT NULL,
                        publication    TEXT NOT NULL,
                        state          TEXT NOT NULL DEFAULT 'active',
                        episode_uuid   TEXT,
                        attempts       INTEGER NOT NULL DEFAULT 0,
                        published_at   TEXT,
                        invalidated_by TEXT,
                        canon_version  INTEGER NOT NULL DEFAULT 1,
                        source_event   TEXT,
                        PRIMARY KEY (group_id, decision_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hash ON publications(group_id, decision_hash)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_episode ON publications(group_id, episode_uuid)"
                )
                conn.commit()
                _conn = conn
    return _conn


def get(group_id: str, decision_id: str) -> Entry | None:
    row = _db().execute(
        f"SELECT {_COLS} FROM publications WHERE group_id = ? AND decision_id = ?",
        (group_id, decision_id),
    ).fetchone()
    return Entry(*row) if row else None


def get_by_hash(group_id: str, decision_hash: str) -> Entry | None:
    row = _db().execute(
        f"SELECT {_COLS} FROM publications WHERE group_id = ? AND decision_hash = ?",
        (group_id, decision_hash),
    ).fetchone()
    return Entry(*row) if row else None


def invalid_episodes(group_id: str) -> set[str]:
    """Episodios cuja decisao foi substituida ou retratada.

    E o que o `recall` usa para nao devolver como vigente algo que deixou de
    valer. Barato: uma consulta, indexada, e o conjunto e pequeno.
    """
    rows = _db().execute(
        "SELECT episode_uuid FROM publications "
        "WHERE group_id = ? AND state IN (?, ?) AND episode_uuid IS NOT NULL",
        (group_id, SUPERSEDED, RETRACTED),
    ).fetchall()
    return {r[0] for r in rows if r[0]}


def mark_attempt(group_id: str, decision_id: str, decision_hash: str, source_event: str | None) -> None:
    db = _db()
    with _lock:
        db.execute(
            """
            INSERT INTO publications
                (group_id, decision_id, decision_hash, publication, state, attempts,
                 canon_version, source_event)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(group_id, decision_id) DO UPDATE SET
                attempts = attempts + 1,
                decision_hash = excluded.decision_hash,
                publication = CASE WHEN publication = ? THEN publication ELSE ? END
            """,
            (group_id, decision_id, decision_hash, PENDING, ACTIVE,
             CANON_VERSION, source_event, PUBLISHED, PENDING),
        )
        db.commit()


def mark_published(group_id: str, decision_id: str, episode_uuid: str, when: str) -> None:
    db = _db()
    with _lock:
        db.execute(
            "UPDATE publications SET publication = ?, episode_uuid = ?, published_at = ? "
            "WHERE group_id = ? AND decision_id = ?",
            (PUBLISHED, episode_uuid, when, group_id, decision_id),
        )
        db.commit()


def mark_failed(group_id: str, decision_id: str) -> None:
    db = _db()
    with _lock:
        db.execute(
            "UPDATE publications SET publication = ? WHERE group_id = ? AND decision_id = ?",
            (FAILED, group_id, decision_id),
        )
        db.commit()


def invalidate(group_id: str, target_decision_id: str, new_state: str, by: str) -> str:
    """Aplica supersede/retract. Nao apaga nada — o historico segue auditavel.

    Devolve um codigo: 'ok', 'not_found' ou 'already_invalid'. Invalidar duas
    vezes e erro explicito, como pedido — nao silencio.
    """
    entry = get(group_id, target_decision_id)
    if entry is None:
        return "not_found"
    if entry.state in INVALID_STATES:
        return "already_invalid"
    db = _db()
    with _lock:
        db.execute(
            "UPDATE publications SET state = ?, invalidated_by = ? "
            "WHERE group_id = ? AND decision_id = ?",
            (new_state, by, group_id, target_decision_id),
        )
        db.commit()
    return "ok"


def history(group_id: str) -> list[dict]:
    rows = _db().execute(
        f"SELECT {_COLS} FROM publications WHERE group_id = ? ORDER BY published_at",
        (group_id,),
    ).fetchall()
    return [Entry(*r).__dict__ for r in rows]


def stats(group_id: str) -> dict[str, int]:
    rows = _db().execute(
        "SELECT state, COUNT(*) FROM publications WHERE group_id = ? GROUP BY state",
        (group_id,),
    ).fetchall()
    return {state: count for state, count in rows}


def reset_for_tests() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
