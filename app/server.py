"""MCP server de memoria em grafo (Graphiti), com isolamento por projeto.

Transporte HTTP streamable em /mcp. O projeto vem do header X-Project-Id ou do
prefixo /p/<projeto>/mcp — nunca do modelo.

Modo cross-project (`X-Project-Id: _all`), pensado para o Cowork:
  - leitura varre todos os projetos, cada resultado rotulado com o seu
  - ESCRITA exige `project` explicito e validado contra os projetos existentes

A assimetria e deliberada: leitura errada devolve resultado irrelevante;
escrita errada contamina um projeto por semanas.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any

from graphiti_core.nodes import EpisodeType
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.responses import JSONResponse

from . import ledger, redaction
from .config import env, hydrate_env
from .auth import ScopeMiddleware
from .graph_store import get_client, shape_fact, shape_node
from .ingest import handle_ingest
from .registry import load_registry
from .scope import (
    ALL_PROJECTS,
    READ,
    WRITE,
    PermissionDenied,
    ScopeError,
    current_scope,
    normalize_project_id,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("graphmem")

# Antes de qualquer coisa: segredo em Swarm chega como arquivo, e o SDK da
# OpenAI so olha o ambiente.
hydrate_env()

# Hosts sempre aceitos: o healthcheck do container bate em 127.0.0.1:8080, e a
# suite roda sobre ASGI com Host de loopback. Nunca chegam pela internet — o
# `cloudflared` reescreve o Host com o hostname publico configurado no tunel.
_HOSTS_LOCAIS = ("127.0.0.1", "localhost", "[::1]")


def transport_security() -> TransportSecuritySettings:
    """Protecao contra DNS rebinding, com o hostname publico declarado.

    O SDK LIGA esta protecao sozinho quando `FastMCP(host=...)` fica no default
    `127.0.0.1`, e a lista permitida vira so loopback. Atras do Cloudflare
    Tunnel o Host que chega e o hostname publico, entao TODO request de
    producao morria em 421 "Invalid Host header" — antes do nosso middleware,
    antes do registro de projeto, antes de qualquer log nosso.

    Nenhum teste pegava: todos batem no ASGI com Host de loopback, que e
    justamente o valor permitido. So aparecia com hostname real na frente.

    `MCP_ALLOWED_HOSTS` e obrigatoria em AUTH_MODE=cloudflare, de proposito:
    o modo cloudflare so existe atras de proxy, e esquecer a variavel voltaria
    a produzir 421 em producao. Boot que falha e mais barato que 421 mudo.
    """
    raw = (env("MCP_ALLOWED_HOSTS") or "").strip()
    publicos = [h.lower() for h in re.split(r"[,\s]+", raw) if h]

    modo = (os.environ.get("AUTH_MODE", "cloudflare") or "").strip().lower()
    if modo == "cloudflare" and not publicos:
        raise RuntimeError(
            "MCP_ALLOWED_HOSTS nao definido com AUTH_MODE=cloudflare. "
            "Declare o hostname publico do tunel (ex: "
            "MCP_ALLOWED_HOSTS=memoria.SEU.DOMINIO). Sem isso o SDK aceita "
            "apenas Host de loopback e todo request pelo tunel responde 421."
        )

    hosts = [*publicos, *_HOSTS_LOCAIS]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # `h:*` cobre Host com porta explicita; `h` cobre sem porta.
        allowed_hosts=[*hosts, *(f"{h}:*" for h in hosts)],
        # Origin ausente passa (cliente MCP nao e browser). Quando vier, so
        # vale o proprio hostname — https na borda, http so no loopback.
        allowed_origins=[
            *(f"https://{h}" for h in publicos),
            *(f"https://{h}:*" for h in publicos),
            *(f"http://{h}:*" for h in _HOSTS_LOCAIS),
        ],
    )


mcp = FastMCP(
    "elephant-memory",
    transport_security=transport_security(),
    instructions=(
        "Memoria de longo prazo em grafo temporal, isolada por projeto. O projeto "
        "atual vem da conexao — voce NAO precisa e NAO deve informa-lo, exceto "
        "quando uma tool disser explicitamente que ele e obrigatorio. "
        "Use `remember` para decisoes, convencoes e restricoes que valham para "
        "sessoes futuras. Use `recall` no inicio de uma tarefa. Os fatos trazem "
        "`valid_at` e `invalid_at`: um `invalid_at` preenchido significa que "
        "aquilo JA FOI verdade e deixou de ser — nunca apresente isso como atual."
    ),
)


async def _known_projects() -> list[str]:
    """Projetos visiveis para ESTA credencial.

        registro declarado  ∩  projetos com permissao de leitura

    O registro e a autoridade. As versoes anteriores inferiam do banco — por no
    `:Entity`, depois por qualquer no — e cada uma errava para um lado: a
    primeira apagava da lista um projeto ingerido sem extracao de entidade; a
    segunda deixava um grafo criado por engano virar projeto.

    Com o registro, projeto registrado e vazio continua visivel, e grafo orfao
    nunca aparece. O probe de "tem no" desceu para health check (`/healthz`).
    """
    scope = current_scope()
    registro = load_registry()
    legiveis = scope.projects_for(READ)
    if not registro:
        # Sem PROJECTS declarado, a permissao e a unica fonte — util em dev.
        return sorted(legiveis)
    return sorted(registro & legiveis)


async def _resolve_write_target(project: str | None) -> str:
    """Decide onde gravar e confere permissao. Falha ruidosamente quando ambiguo."""
    scope = current_scope()
    registro = load_registry()

    if scope.is_cross_project:
        if not project:
            raise ScopeError(
                "Esta conexao esta em modo de leitura ampla (_all). Para gravar, "
                f"informe `project`. Voce pode gravar em: "
                f"{', '.join(sorted(scope.projects_for(WRITE))) or '(nenhum)'}."
            )
        alvo = normalize_project_id(project)
        if alvo == ALL_PROJECTS:
            raise ScopeError("'_all' nao e um projeto gravavel.")
    else:
        if project and normalize_project_id(project) != scope.project_id:
            raise ScopeError(
                f"Esta conexao esta presa ao projeto '{scope.project_id}'. "
                f"Nao e possivel gravar em '{project}' daqui."
            )
        alvo = scope.project_id

    if registro and alvo not in registro:
        raise ScopeError(
            f"Projeto '{alvo}' nao esta registrado. Criar projeto e ato "
            f"administrativo. Registrados: {', '.join(sorted(registro))}."
        )
    scope.require(WRITE, alvo)
    return alvo


# --------------------------------------------------------------------------- #
# Escrita
# --------------------------------------------------------------------------- #


@mcp.tool()
async def remember(
    content: Annotated[
        str,
        Field(description="A decisao, convencao ou restricao a memorizar. Frase completa e autocontida."),
    ],
    name: Annotated[str, Field(description="Titulo curto do episodio, para auditoria.")] = "nota",
    project: Annotated[
        str | None,
        Field(description="So em conexoes de leitura ampla (_all): projeto de destino. Caso contrario, deixe vazio."),
    ] = None,
) -> dict[str, Any]:
    """Grava no grafo do projeto.

    O Graphiti extrai entidades e relacoes, e invalida fatos anteriores que
    passem a ser contraditos. Custa varias chamadas de LLM — use para o que
    vale lembrar daqui a meses, nao para estado da sessao.
    """
    scope = current_scope()
    try:
        target = await _resolve_write_target(project)
    except (ScopeError, PermissionDenied) as exc:
        return {"stored": False, "error": str(exc)}

    # O caminho /ingest herda o gate pre-commit do `.ia`. Este aqui nao tem
    # gate nenhum a montante, e o texto vai para um LLM externo na extracao.
    try:
        redaction.assert_clean(content, context="conteudo")
    except ValueError as exc:
        logger.warning("remember recusado por politica em project=%s", target)
        return {"stored": False, "error": str(exc)}

    client = await get_client()
    await client.add_episode(
        name=name,
        episode_body=content,
        source=EpisodeType.text,
        source_description="mcp:remember",
        reference_time=datetime.now(timezone.utc),
        group_id=target,
    )
    logger.info("remember project=%s user=%s", target, scope.user_id)
    return {"stored": True, "project": target}


# --------------------------------------------------------------------------- #
# Leitura
# --------------------------------------------------------------------------- #


def drop_invalidated(edges, invalid_episodes: set[str]) -> list:
    """Remove fatos cujas UNICAS origens foram substituidas ou retratadas.

    Se ao menos um episodio de origem continua ativo, o fato fica: uma aresta
    pode ter sido reforcada por mais de um episodio, e derrubar por causa de um
    so apagaria conhecimento vigente.

    Fato sem origem declarada tambem fica — nao ha base para invalidar.
    """
    saida = []
    for edge in edges:
        origens = {str(u) for u in (getattr(edge, "episodes", None) or [])}
        if origens and origens <= invalid_episodes:
            continue
        saida.append(edge)
    return saida


async def _search(query: str, group_ids: list[str], limit: int) -> list[dict[str, Any]]:
    """Busca sempre com a lista de projetos EXPLICITA.

    Nunca passe `group_ids=None` aqui. No FalkorDB, cada projeto e um grafo
    separado, e o Graphiti so faz fan-out entre grafos quando recebe a lista;
    com None, ele varre apenas o grafo corrente e devolve um resultado
    silenciosamente parcial. `_scope_groups()` resolve a lista certa.
    """
    if not group_ids:
        return []
    client = await get_client()
    # Pede folga: fatos vindos de decisao invalidada sao descartados depois, e
    # sem folga o corte devolveria menos que o pedido.
    edges = await client.search(query=query, group_ids=group_ids, num_results=limit * 3)

    # O ledger — nao o LLM — decide o que ainda vale. Um fato cujas UNICAS
    # origens foram substituidas ou retratadas nao volta como vigente. Se ao
    # menos um episodio de origem continua ativo, o fato fica.
    invalidos: set[str] = set()
    for gid in group_ids:
        invalidos |= ledger.invalid_episodes(gid)

    return [shape_fact(e) for e in drop_invalidated(edges, invalidos)][:limit]


async def _scope_groups() -> list[str]:
    """Projetos que a conexao atual pode ler.

    Conexao presa a projeto sem memoria devolve lista vazia em vez de buscar.
    Isso corta o fantasma na origem: sem isso, um `recall` com header errado
    criaria o grafo (o FalkorDB cria no primeiro acesso, ate em leitura) e o
    nome entraria na lista de projetos conhecidos pela porta da leitura.

    Escrita NAO passa por aqui: e assim que um projeto novo nasce.
    """
    scope = current_scope()
    known = await _known_projects()
    if scope.is_cross_project:
        return known
    return [scope.project_id] if scope.project_id in known else []


@mcp.tool()
async def recall(
    query: Annotated[
        str,
        Field(description=(
            "O que voce quer lembrar, como FRASE em linguagem natural — nao "
            "palavra-chave solta. 'qual broker de mensageria usamos?' funciona; "
            "'broker' pode nao casar com um fato longo."
        )),
    ],
    limit: Annotated[int, Field(description="Maximo de fatos.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Busca fatos do projeto atual.

    Fatos de decisoes substituidas ou retratadas NAO sao devolvidos: o estado
    de vigencia vem do ledger, nao da extracao do LLM.

    Em conexao de leitura ampla (_all), varre todos os projetos e rotula cada
    resultado com o projeto de origem.
    """
    scope = current_scope()
    groups = await _scope_groups()
    facts = await _search(query, groups, limit)
    return {
        "scope": "todos os projetos" if scope.is_cross_project else scope.project_id,
        "projects_searched": groups if scope.is_cross_project else None,
        "count": len(facts),
        "facts": facts,
    }


@mcp.tool()
async def recall_project(
    query: Annotated[str, Field(description="O que procurar.")],
    project: Annotated[str, Field(description="Nome do projeto. Use `list_projects` se nao souber o nome exato.")],
    limit: Annotated[int, Field(description="Maximo de fatos.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Busca em um projeto nomeado.

    E assim que se responde "sobre o projeto X, o que a gente decidiu?" a partir
    de uma conexao de leitura ampla. Nome inexistente devolve erro com a lista
    de projetos validos — nunca uma lista vazia silenciosa, que se confunde com
    "nao ha nada gravado".
    """
    try:
        target = normalize_project_id(project)
    except ScopeError as exc:
        return {"error": str(exc)}

    scope = current_scope()
    if not scope.is_cross_project and target != scope.project_id:
        return {
            "error": f"Esta conexao esta presa ao projeto '{scope.project_id}'. "
                     f"Use uma conexao de leitura ampla para consultar '{target}'."
        }

    known = await _known_projects()
    if target not in known:
        return {
            "error": f"Projeto '{target}' nao encontrado.",
            "projetos_disponiveis": known,
        }

    facts = await _search(query, [target], limit)
    return {"project": target, "count": len(facts), "facts": facts}


@mcp.tool()
async def recall_other_projects(
    query: Annotated[str, Field(description="O que procurar nos demais projetos.")],
    limit: Annotated[int, Field(description="Maximo de fatos.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Busca nos outros projetos, excluindo o atual.

    Escotilha deliberada do isolamento, para "como eu resolvi isso no outro
    projeto?". Cada fato vem com o campo `project` — cite-o, porque o contexto
    de la pode nao valer aqui.
    """
    scope = current_scope()
    if scope.is_cross_project:
        return {"error": "Esta conexao ja le todos os projetos. Use `recall`."}

    others = [p for p in await _known_projects() if p != scope.project_id]
    if not others:
        return {"count": 0, "facts": [], "note": "Nao ha outros projetos com memoria."}

    facts = await _search(query, others, limit)
    return {"excluded_project": scope.project_id, "count": len(facts), "facts": facts}


@mcp.tool()
async def search_entities(
    query: Annotated[str, Field(description="Entidade ou conceito a procurar (servico, tecnologia, pessoa).")],
    limit: Annotated[int, Field(description="Maximo de entidades.", ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """Busca ENTIDADES (nos do grafo), nao fatos.

    Use quando a pergunta e sobre uma coisa e nao sobre uma afirmacao: "o que a
    gente sabe sobre o Kafka aqui?". Devolve o resumo acumulado de cada entidade
    a partir de tudo que ja foi gravado.
    """
    from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF

    scope = current_scope()
    groups = await _scope_groups()
    if not groups:
        return {"scope": "nenhum projeto com memoria", "entities": []}
    client = await get_client()
    config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True)
    config.limit = limit
    results = await client.search_(query=query, config=config, group_ids=groups)
    return {
        "scope": "todos os projetos" if scope.is_cross_project else scope.project_id,
        "entities": [shape_node(n) for n in getattr(results, "nodes", [])],
    }


@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """Lista os projetos que ja tem memoria gravada.

    Chame antes de `recall_project` quando nao tiver certeza do nome exato.
    """
    scope = current_scope()
    return {
        "current": "leitura ampla (_all)" if scope.is_cross_project else scope.project_id,
        "projects": await _known_projects(),
    }


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request):
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ingest", methods=["POST"])
async def ingest(request):
    """Porta dos projetos com `.ia/`: publicacao determinística pelo handoff.

    Exige o grant `ingest`, que e separado de `write` de proposito — o token do
    publicador nao precisa poder chamar o MCP, e o token pessoal do Cowork nao
    precisa poder publicar handoff.
    """
    return await handle_ingest(request)


def build_app():
    return ScopeMiddleware(mcp.streamable_http_app())


app = build_app()
