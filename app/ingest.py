"""Endpoint REST de ingestao — a porta dos projetos com `.ia/`.

Recebe do publicador as decisoes de um evento de handoff ja commitado. Quem
seleciona e o script; o modelo apenas executa o comando.

Contrato: o servidor **recalcula** o hash canonico e ignora o que o cliente
mandou (aceita para conferencia). A canonicalizacao precisa ser de um lado so,
senao dois publicadores com normalizacao diferente duplicam o mesmo fato.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from graphiti_core.nodes import EpisodeType
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import ledger, redaction
from .graph_store import get_client
from .registry import load_registry
from .scope import ALL_PROJECTS, INGEST, PermissionDenied, current_scope, normalize_project_id

logger = logging.getLogger("graphmem.ingest")

INGESTIBLE_STATUS = {"ready", "completed"}
MAX_DECISIONS = 200

OPERATIONS = {"add", "supersede", "retract"}

# Estados de resultado, por decisao.
PUBLISHED = "published"
ALREADY = "already_published"
REJECTED = "rejected"
FAILED = "failed"


class IngestError(ValueError):
    pass


def _reference_time(raw: str | None) -> datetime:
    """`created_at` do evento vira `reference_time` do episodio.

    Nao e a hora da ingestao: e isso que faz a bitemporalidade valer.
    Reindexar amanha nao muda quando a decisao foi tomada.
    """
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError(f"created_at invalido: {raw!r} ({exc})") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate(raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Valida uma decisao. Devolve (normalizada, reason_code se invalida)."""
    decision_id = str(raw.get("decision_id") or "").strip()
    if not decision_id:
        return {}, "missing_decision_id"

    operation = str(raw.get("operation") or "add").strip().lower()
    if operation not in OPERATIONS:
        return {}, "invalid_operation"

    statement = raw.get("statement")
    statement = str(statement).strip() if statement is not None else None
    target = str(raw.get("target_decision_id") or "").strip() or None

    if operation == "add":
        if not statement:
            return {}, "add_requires_statement"
        if target:
            return {}, "add_forbids_target"
    elif operation == "supersede":
        if not statement:
            return {}, "supersede_requires_statement"
        if not target:
            return {}, "supersede_requires_target"
    elif operation == "retract":
        if statement:
            return {}, "retract_forbids_statement"
        if not target:
            return {}, "retract_requires_target"

    refs = raw.get("authority_refs") or []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        return {}, "invalid_authority_refs"

    return {
        "decision_id": decision_id,
        "operation": operation,
        "statement": statement,
        "rationale": str(raw.get("rationale") or "").strip() or None,
        "authority_refs": [str(r).strip() for r in refs if str(r).strip()],
        "target_decision_id": target,
    }, None


def _episode_body(d: dict[str, Any]) -> str:
    partes: list[str] = []
    if d["operation"] == "retract":
        partes.append(
            f"A decisao {d['target_decision_id']} foi CANCELADA e nao vale mais. "
            "Nao existe decisao substituta."
        )
    elif d["operation"] == "supersede":
        partes.append(
            f"Esta decisao substitui a {d['target_decision_id']}, que deixou de valer."
        )
        partes.append(d["statement"] or "")
    else:
        partes.append(d["statement"] or "")

    if d["rationale"]:
        partes.append(f"Motivo: {d['rationale']}")
    if d["authority_refs"]:
        partes.append(f"Fonte: {', '.join(d['authority_refs'])}")
    return " ".join(p for p in partes if p)


async def handle_ingest(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Corpo nao e JSON valido."}, status_code=400)

    try:
        result = await _ingest(payload, current_scope())
    except PermissionDenied as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except IngestError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    houve_falha = any(r["status"] in (REJECTED, FAILED) for r in result["results"])
    return JSONResponse(result, status_code=207 if houve_falha else 200)


async def _ingest(payload: dict[str, Any], scope) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IngestError("Esperado um objeto JSON.")

    source = payload.get("source") or {}
    if not isinstance(source, dict):
        raise IngestError("'source' deve ser um objeto.")

    # Projeto: vem da conexao. Em _all, o payload precisa dizer qual.
    if scope.is_cross_project:
        declarado = payload.get("project")
        if not declarado:
            raise IngestError("Conexao em modo _all: informe 'project' no payload.")
        target = normalize_project_id(str(declarado))
        if target == ALL_PROJECTS:
            raise IngestError("'_all' nao e um projeto gravavel.")
    else:
        declarado = payload.get("project")
        if declarado and normalize_project_id(str(declarado)) != scope.project_id:
            raise IngestError(
                f"Esta credencial esta presa ao projeto '{scope.project_id}'; "
                f"o payload pede '{declarado}'."
            )
        target = scope.project_id

    registro = load_registry()
    if registro and target not in registro:
        raise IngestError(
            f"Projeto '{target}' nao esta registrado. Registrados: {sorted(registro)}."
        )

    scope.require(INGEST, target)

    event_status = str(source.get("status", "ready")).strip().lower()
    if event_status not in INGESTIBLE_STATUS:
        return {
            "project": target,
            "event_id": source.get("event_id"),
            "skipped_event": True,
            "reason_code": f"status_{event_status}",
            "results": [],
        }

    decisions = payload.get("decisions") or []
    if not isinstance(decisions, list):
        raise IngestError("'decisions' deve ser uma lista.")
    if len(decisions) > MAX_DECISIONS:
        raise IngestError(f"Maximo de {MAX_DECISIONS} decisoes por chamada.")

    reference_time = _reference_time(source.get("created_at"))
    event_id = str(source.get("event_id") or "")
    from_agent = str(source.get("from_agent") or "desconhecido")
    branch = str(source.get("branch") or "")
    sequence = source.get("sequence")

    results: list[dict[str, Any]] = []
    client = await get_client()

    for raw in decisions:
        if not isinstance(raw, dict):
            results.append({"decision_id": None, "status": REJECTED,
                            "reason_code": "not_an_object"})
            continue

        d, motivo = _validate(raw)
        if motivo:
            results.append({"decision_id": raw.get("decision_id"), "status": REJECTED,
                            "reason_code": motivo})
            continue

        digest = ledger.canonical_hash(project_id=target, **d)

        alegado = raw.get("hash")
        if alegado and str(alegado) != digest:
            results.append({"decision_id": d["decision_id"], "status": REJECTED,
                            "reason_code": "hash_mismatch", "server_hash": digest})
            continue

        existente = ledger.get(target, d["decision_id"])
        if existente and existente.publication == ledger.PUBLISHED:
            if existente.decision_hash == digest:
                results.append({"decision_id": d["decision_id"], "status": ALREADY,
                                "episode_uuid": existente.episode_uuid})
            else:
                # Mesmo ID com conteudo diferente: o ID e imutavel por contrato.
                results.append({"decision_id": d["decision_id"], "status": REJECTED,
                                "reason_code": "decision_id_reused_with_different_content"})
            continue

        body = _episode_body(d)
        try:
            redaction.assert_clean(body, context=f"decisao {d['decision_id']}")
        except ValueError:
            logger.warning("decisao %s recusada por politica de conteudo", d["decision_id"])
            results.append({"decision_id": d["decision_id"], "status": REJECTED,
                            "reason_code": "possible_secret_or_pii"})
            continue

        # Invalidacao e aplicada ANTES de publicar: se o alvo nao existe ou ja
        # foi invalidado, a decisao inteira e recusada em vez de publicar um
        # episodio que afirma substituir algo inexistente.
        if d["operation"] in ("supersede", "retract"):
            novo_estado = ledger.SUPERSEDED if d["operation"] == "supersede" else ledger.RETRACTED
            codigo = ledger.invalidate(target, d["target_decision_id"], novo_estado,
                                       d["decision_id"])
            if codigo != "ok":
                results.append({"decision_id": d["decision_id"], "status": REJECTED,
                                "reason_code": f"target_{codigo}"})
                continue

        ledger.mark_attempt(target, d["decision_id"], digest, event_id or None)

        try:
            resultado = await client.add_episode(
                name=f"{d['operation']}:{d['decision_id']}",
                episode_body=body,
                source=EpisodeType.text,
                source_description=(
                    f"handoff event={event_id} seq={sequence} "
                    f"agent={from_agent} branch={branch}"
                ).strip(),
                reference_time=reference_time,
                group_id=target,
            )
            episode_uuid = str(getattr(getattr(resultado, "episode", None), "uuid", "") or "")
            ledger.mark_published(target, d["decision_id"], episode_uuid,
                                  datetime.now(timezone.utc).isoformat())
            entrada = {"decision_id": d["decision_id"], "status": PUBLISHED,
                       "episode_uuid": episode_uuid}
            if d["target_decision_id"]:
                entrada["invalidated"] = d["target_decision_id"]
            results.append(entrada)
        except Exception as exc:  # noqa: BLE001
            ledger.mark_failed(target, d["decision_id"])
            logger.exception("falha ao publicar %s em %s", d["decision_id"], target)
            results.append({"decision_id": d["decision_id"], "status": FAILED,
                            "reason_code": "transport_or_extraction_error",
                            "detail": str(exc)[:200]})

    logger.info(
        "ingest project=%s event=%s %s", target, event_id,
        {s: sum(1 for r in results if r["status"] == s)
         for s in (PUBLISHED, ALREADY, REJECTED, FAILED)},
    )
    return {
        "project": target,
        "event_id": event_id,
        "canonicalization_version": ledger.CANON_VERSION,
        "results": results,
        "ledger": ledger.stats(target),
    }
