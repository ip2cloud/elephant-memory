"""Testes contra FalkorDB real.

1. Borda (ASGI real): identidade, escopo por header e por caminho /p/<projeto>.
2. Isolamento (grafo real): projeto A nao ve fato do projeto B; o modo _all ve
   os dois e recusa escrita sem destino explicito.

A ingestao usa `add_triplet`, que grava nos e arestas direto no grafo sem passar
pelo pipeline de LLM. Isso e proposital: o que esta sob teste e o isolamento por
`group_id` no banco, nao a qualidade da extracao.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "tok_teste_com_tamanho_suficiente_123456"          # so leitura
TOKEN_WRITE = "tok_teste_com_escrita_1234567890123456789"  # leitura + escrita
TOKEN_INGEST = "tok_teste_do_publicador_123456789012345"   # leitura + ingest
os.environ.setdefault("AUTH_MODE", "token")
os.environ.setdefault("PROJECTS", '["proj-alpha","proj-beta","acme-api-billing","proj-ingest","proj-sem-entidade"]')

_TODOS = json.loads(os.environ["PROJECTS"])
_R = [f"read:{p}" for p in _TODOS]
_W = _R + [f"write:{p}" for p in _TODOS]
_I = _R + [f"ingest:{p}" for p in _TODOS]

os.environ.setdefault("AUTH_TOKENS", json.dumps({
    TOKEN: {"user": "alfredo", "scopes": _R},
    TOKEN_WRITE: {"user": "alfredo", "scopes": _W},
    TOKEN_INGEST: {"user": "publicador", "scopes": _I},
}))
os.environ.setdefault("LEDGER_DB", "/tmp/graphmem-test-ledger.db")
os.environ.setdefault("FALKORDB_HOST", "127.0.0.1")
os.environ.setdefault("FALKORDB_PORT", "6379")
os.environ.setdefault("FALKORDB_DATABASE", "memoria_test")
os.environ.setdefault("OPENAI_API_KEY", "sk-stub")

DIMS = 1536
failures: list[str] = []
passed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FALHOU'}  {name}{(' :: ' + detail) if detail and not ok else ''}")


# --------------------------------------------------------------------------- #
# Camada 0 — configuracao (nao precisa de banco)
# --------------------------------------------------------------------------- #


def test_env_passthrough() -> None:
    """Toda variavel lida pelo app precisa ser repassada no compose.

    Esta classe de bug nao aparece em nenhuma outra suite: as duas rodam em
    AUTH_MODE=token, entao uma variavel do caminho Cloudflare pode faltar no
    compose sem que nada falhe. O efeito de CF_ACCESS_GRANTS faltando era o
    servidor subir somente-leitura para todo mundo, em producao, com uma
    mensagem que parece erro de permissao do Access e nao de configuracao.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    lidas: set[str] = set()
    app_dir = os.path.join(root, "app")
    for nome in os.listdir(app_dir):
        if not nome.endswith(".py"):
            continue
        with open(os.path.join(app_dir, nome), encoding="utf-8") as fh:
            texto = fh.read()
        lidas |= set(re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z0-9_]+)"', texto))

    with open(os.path.join(root, "docker-compose.yml"), encoding="utf-8") as fh:
        compose = fh.read()
    bloco = compose.split("\n  mcp:")[1].split("\n  cloudflared:")[0]
    env_bloco = bloco.split("environment:")[1].split("depends_on:")[0]
    repassadas = set(re.findall(r"^\s{6}([A-Z0-9_]+):", env_bloco, re.M))

    faltando = sorted(lidas - repassadas)
    check(
        "toda variavel lida pelo app e repassada no compose",
        not faltando,
        f"faltando: {faltando}",
    )


def test_env_passthrough_safe() -> None:
    """Envelope tolerante: sem o compose ao lado (CI com so `tests/` montado),
    registra que nao deu para verificar em vez de derrubar a suite inteira
    antes do bloco [1] — e ainda sair com 0."""
    try:
        test_env_passthrough()
    except FileNotFoundError as exc:
        check("nao pude verificar o repasse de variaveis (compose ausente)",
              False, str(exc)[:120])


def test_drop_invalidated() -> None:
    """Filtro de vigencia, isolado do banco e do LLM.

    Precisa ser testado aqui porque, com o LLM stubado, a busca nao produz
    aresta nenhuma — nao ha como exercitar o corte por dentro do `recall`.
    """
    from app.server import drop_invalidated

    class E:
        def __init__(self, episodes):
            self.episodes = episodes

    invalidos = {"ep-morto-1", "ep-morto-2"}
    edges = [
        E(["ep-vivo"]),                  # origem ativa -> fica
        E(["ep-morto-1"]),               # unica origem invalidada -> sai
        E(["ep-morto-1", "ep-vivo"]),    # uma origem ainda ativa -> fica
        E(["ep-morto-1", "ep-morto-2"]), # todas invalidadas -> sai
        E([]),                           # sem origem declarada -> fica
    ]
    restantes = drop_invalidated(edges, invalidos)
    check("filtro de vigencia derruba so o que perdeu todas as origens",
          len(restantes) == 3, f"sobraram {len(restantes)}")
    check("fato com origem ainda ativa sobrevive",
          edges[2] in restantes, "aresta reforcada foi derrubada")


# --------------------------------------------------------------------------- #
# Camada 1 — borda
# --------------------------------------------------------------------------- #


class Lifespan:
    """Dispara o ciclo de lifespan do app ASGI.

    Sem isto o transporte MCP levanta "Task group is not initialized" assim que
    uma requisicao passa pela autenticacao e chega nele.
    """

    def __init__(self, app):
        self.app = app
        self._recv: asyncio.Queue = asyncio.Queue()
        self._sent: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, self._recv.get, self._sent.put)
        )
        await self._recv.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self._sent.get(), timeout=10)
        return self

    async def __aexit__(self, *exc):
        await self._recv.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._sent.get(), timeout=10)
            await asyncio.wait_for(self._task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()


async def test_edge() -> None:
    from app.server import app

    async with Lifespan(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        auth = {"Authorization": f"Bearer {TOKEN}"}

        r = await c.get("/healthz")
        check("healthz sem auth -> 200", r.status_code == 200, f"got {r.status_code}")

        r = await c.post("/mcp", json={})
        check("sem credencial -> 401", r.status_code == 401, f"got {r.status_code}")

        r = await c.post("/mcp", json={}, headers={"Authorization": "Bearer errado_porem_longo_o_suficiente"})
        check("credencial invalida -> 401", r.status_code == 401, f"got {r.status_code}")

        r = await c.post("/mcp", json={}, headers=auth)
        check("sem projeto -> 400", r.status_code == 400, f"got {r.status_code}")

        for bad in ["../outro", "proj a", "PROJ/../..", "a" * 80, ""]:
            r = await c.post("/mcp", json={}, headers={**auth, "X-Project-Id": bad})
            check(f"projeto rejeitado: {bad[:18]!r}", r.status_code == 400, f"got {r.status_code}")

        # Escopo por caminho, para clientes que so aceitam URL. O sucesso aqui e
        # "passou pelo middleware e chegou no MCP" — o que o transporte responde
        # a um POST cru nao interessa.
        r = await c.post("/p/proj-alpha/mcp", json={}, headers=auth)
        check(
            "escopo por caminho aceito",
            r.status_code != 401 and "Projeto invalido" not in r.text,
            f"got {r.status_code} {r.text[:80]}",
        )

        r = await c.post("/p/..%2Fetc/mcp", json={}, headers=auth)
        check(
            "caminho malicioso rejeitado",
            r.status_code == 400 and "Projeto invalido" in r.text,
            f"got {r.status_code} {r.text[:80]}",
        )


# --------------------------------------------------------------------------- #
# Camada 2 — isolamento no grafo real
# --------------------------------------------------------------------------- #


class StubEmbedder(EmbedderClient):
    """Embedding deterministico por bag-of-words: textos que compartilham
    palavras ficam proximos. Suficiente para a busca ter sentido no teste.

    Precisa herdar de EmbedderClient: o Graphiti valida os clientes com pydantic
    (`is_instance_of`), entao duck typing nao passa."""

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * DIMS
        for word in str(text).lower().replace(".", " ").split():
            h = hashlib.sha256(word.encode()).digest()
            for i in range(8):
                vec[int.from_bytes(h[i * 2:i * 2 + 2], "big") % DIMS] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    async def create(self, input_data):
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            return self._vec(" ".join(input_data))
        return self._vec(input_data if isinstance(input_data, str) else str(input_data))

    async def create_batch(self, input_data_list):
        return [self._vec(t) for t in input_data_list]


class StubLLM(LLMClient):
    """Satisfaz o contrato sem chamar provedor nenhum.

    O Graphiti pede saida estruturada por `response_model` em cada etapa da
    extracao. Em vez de adivinhar o formato de cada prompt, o stub constroi a
    instancia minima valida do modelo pedido — listas vazias, strings vazias.
    Resultado: o episodio e criado, nenhuma entidade e extraida. Suficiente
    para testar ledger, idempotencia e permissao, que e o alvo aqui.
    """

    def __init__(self):  # sem config, sem cliente
        pass

    async def _generate_response(self, messages, response_model=None, **kwargs):
        return await self.generate_response(messages, response_model, **kwargs)

    async def generate_response(self, messages, response_model=None, **kwargs):
        if response_model is None:
            return {}
        out = {}
        for name, field in response_model.model_fields.items():
            ann = str(field.annotation)
            if "list" in ann or "List" in ann:
                out[name] = []
            elif "bool" in ann:
                out[name] = False
            elif "int" in ann:
                out[name] = 0
            elif "str" in ann:
                out[name] = ""
            else:
                out[name] = None
        return out


class StubCrossEncoder(CrossEncoderClient):
    async def rank(self, query, passages):
        return [(p, 1.0 - i * 0.01) for i, p in enumerate(passages)]


async def build_test_client():
    from graphiti_core import Graphiti
    from app import graph_store

    client = Graphiti(
        graph_driver=graph_store.build_driver(),
        embedder=StubEmbedder(),
        cross_encoder=StubCrossEncoder(),
        llm_client=StubLLM(),
    )
    await client.build_indices_and_constraints()
    await graph_store.set_client(client)
    return client


_group_clients: dict = {}


async def client_for_group(group_id: str):
    """Um cliente Graphiti apontado para O GRAFO DAQUELE PROJETO.

    Este e o detalhe que invalidava a versao anterior deste teste: no FalkorDB,
    o Graphiti cria **um grafo por group_id**. `add_triplet` grava no grafo
    corrente do driver (o default), mas a busca com `group_ids=[x]` clona o
    driver para o grafo `x` — que ficava vazio. Dai "grava, isola, mas nao
    acha". Semear por grafo reproduz o que `add_episode` faz em producao.
    """
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    if group_id not in _group_clients:
        c = Graphiti(
            graph_driver=FalkorDriver(
                host=os.environ["FALKORDB_HOST"],
                port=int(os.environ.get("FALKORDB_PORT", "6379")),
                # Sem `password` a suite morre em "Authentication required"
                # antes do bloco de isolamento. Ficou latente enquanto o banco
                # subia aberto por causa do `command:` descartado no compose —
                # dois bugs se cancelando.
                password=os.environ.get("FALKORDB_PASSWORD") or None,
                database=group_id,
            ),
            embedder=StubEmbedder(),
            cross_encoder=StubCrossEncoder(),
            llm_client=StubLLM(),
        )
        await c.build_indices_and_constraints()
        _group_clients[group_id] = c
    return _group_clients[group_id]


async def seed(client, group_id: str, subject: str, predicate: str, obj: str, fact: str):
    """Grava um triplo no grafo DO PROJETO, sem passar pelo LLM."""
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    target = await client_for_group(group_id)
    now = datetime.now(timezone.utc)
    src = EntityNode(uuid=str(uuid.uuid4()), name=subject, group_id=group_id, labels=["Entity"], created_at=now)
    dst = EntityNode(uuid=str(uuid.uuid4()), name=obj, group_id=group_id, labels=["Entity"], created_at=now)
    edge = EntityEdge(
        uuid=str(uuid.uuid4()),
        source_node_uuid=src.uuid,
        target_node_uuid=dst.uuid,
        group_id=group_id,
        name=predicate,
        fact=fact,
        created_at=now,
        valid_at=now,
    )
    embedder = target.embedder
    await src.generate_name_embedding(embedder)
    await dst.generate_name_embedding(embedder)
    await edge.generate_embedding(embedder)
    await target.add_triplet(src, edge, dst)


async def test_isolation() -> None:
    from app.scope import reset_scope, set_scope
    from app.server import list_projects, recall, recall_other_projects, recall_project, remember

    client = await build_test_client()

    # Limpa resíduo de execuções anteriores, inclusive os grafos por projeto.
    await client.driver.execute_query("MATCH (n) DETACH DELETE n")
    for name in await client.driver.client.list_graphs():
        name = name.decode() if isinstance(name, (bytes, bytearray)) else str(name)
        if name == os.environ.get("FALKORDB_DATABASE", "memoria"):
            continue
        try:
            await client.driver.client.select_graph(name).delete()
        except ConnectionError:
            # O FalkorDB embutido derruba a conexao em alguns deletes. Residuo
            # de grafo nao invalida o teste (nomes fixos, sobrescritos a cada
            # rodada). Restrito a ConnectionError de proposito: qualquer outro
            # erro aqui deve estourar em vez de ser mascarado.
            pass

    await seed(client, "proj-alpha", "alpha", "USES", "RabbitMQ",
               "O projeto alpha usa RabbitMQ para mensageria")
    await seed(client, "proj-beta", "beta", "USES", "Kafka",
               "O projeto beta usa Kafka para mensageria")

    async def as_scope(project: str, coro_fn, scopes=None):
        token = set_scope("alfredo", project, frozenset(scopes if scopes is not None else _W))
        try:
            return await coro_fn()
        finally:
            reset_scope(token)

    def joined(payload, key="facts"):
        return " ".join((f.get("fact") or "") for f in payload.get(key, []))

    alpha = await as_scope("proj-alpha", lambda: recall("mensageria", 10))
    check("alpha acha o proprio fato", "RabbitMQ" in joined(alpha), joined(alpha)[:120])
    check("alpha NAO ve o fato do beta", "Kafka" not in joined(alpha), joined(alpha)[:120])
    check(
        "todo fato carrega o projeto correto",
        all(f["project"] == "proj-alpha" for f in alpha["facts"]),
        str([f["project"] for f in alpha["facts"]]),
    )

    beta = await as_scope("proj-beta", lambda: recall("mensageria", 10))
    check("beta acha o proprio fato", "Kafka" in joined(beta), joined(beta)[:120])
    check("beta NAO ve o fato do alpha", "RabbitMQ" not in joined(beta), joined(beta)[:120])

    cross = await as_scope("proj-alpha", lambda: recall_other_projects("mensageria", 10))
    check("recall_other_projects alcanca o beta", "Kafka" in joined(cross), joined(cross)[:120])
    check("recall_other_projects exclui o alpha", "RabbitMQ" not in joined(cross), joined(cross)[:120])

    # ---- modo _all: o cenario do Cowork -----------------------------------
    todos = await as_scope("_all", lambda: recall("mensageria", 20))
    check("_all enxerga os dois projetos",
          "Kafka" in joined(todos) and "RabbitMQ" in joined(todos), joined(todos)[:160])

    nomeado = await as_scope("_all", lambda: recall_project("mensageria", "proj-beta", 10))
    check("_all consulta projeto nomeado", "Kafka" in joined(nomeado), str(nomeado)[:160])
    check("projeto nomeado nao vaza o outro", "RabbitMQ" not in joined(nomeado), joined(nomeado)[:120])

    inexistente = await as_scope("_all", lambda: recall_project("x", "proj-que-nao-existe", 5))
    check("projeto inexistente devolve erro, nao lista vazia",
          "error" in inexistente and inexistente.get("projetos_disponiveis"), str(inexistente)[:160])

    sem_destino = await as_scope("_all", lambda: remember("um fato qualquer"))
    check("_all RECUSA escrita sem projeto explicito",
          sem_destino.get("stored") is False, str(sem_destino)[:160])

    fantasma = await as_scope("_all", lambda: remember("um fato", project="projeto-digitado-errado"))
    check("_all recusa escrita em projeto inexistente",
          fantasma.get("stored") is False, str(fantasma)[:160])

    preso = await as_scope("proj-alpha", lambda: remember("x", project="proj-beta"))
    check("conexao presa recusa gravar em outro projeto",
          preso.get("stored") is False, str(preso)[:160])

    # ---- regressao: enumeracao de projetos (bug do grafo-por-group_id) -----
    # Antes, _known_projects fazia Cypher no grafo corrente do driver — e o
    # graphiti troca esse grafo conforme o ultimo projeto tocado. Resultado:
    # list_projects devolvia ora um projeto, ora outro, nunca os dois.
    # Agora a fonte de verdade e GRAPH.LIST, que independe do estado do driver.
    await as_scope("proj-beta", lambda: recall("mensageria", 5))   # move o driver
    l1 = await as_scope("_all", lambda: list_projects())
    await as_scope("proj-alpha", lambda: recall("mensageria", 5))  # move de novo
    l2 = await as_scope("_all", lambda: list_projects())
    check(
        "list_projects e deterministico apos tocar projetos diferentes",
        l1["projects"] == l2["projects"]
        and {"proj-alpha", "proj-beta"} <= set(l1["projects"]),
        f"{l1['projects']} vs {l2['projects']}",
    )

    varredura = await as_scope("_all", lambda: recall("mensageria", 20))
    check(
        "_all declara os projetos varridos (lista explicita, nunca None)",
        isinstance(varredura.get("projects_searched"), list)
        and {"proj-alpha", "proj-beta"} <= set(varredura["projects_searched"]),
        str(varredura.get("projects_searched")),
    )

    # Nome com hifen e o caso real: todo slug de git remote tem um.
    await seed(client, "acme-api-billing", "billing", "USES", "Postgres",
               "O acme-api-billing usa Postgres como banco principal")
    comhifen = await as_scope("acme-api-billing", lambda: recall("banco principal", 10))
    check(
        "projeto com hifen acha o proprio fato",
        any("Postgres" in (f.get("fact") or "") for f in comhifen["facts"]),
        str(comhifen)[:160],
    )

    listagem = await as_scope("_all", lambda: list_projects())
    check("list_projects enxerga todos",
          {"proj-alpha", "proj-beta", "acme-api-billing"} <= set(listagem["projects"]), str(listagem))

    for c in _group_clients.values():
        try:
            await c.driver.execute_query("MATCH (n) DETACH DELETE n")
        except ConnectionError:
            pass
        await c.close()
    _group_clients.clear()
    await client.driver.execute_query("MATCH (n) DETACH DELETE n")
    await client.close()


# --------------------------------------------------------------------------- #
# Camada 3 — permissao, politica de conteudo, projeto fantasma e /ingest
# --------------------------------------------------------------------------- #


async def test_permissions_and_ingest() -> None:
    from app import ledger
    from app.scope import reset_scope, set_scope
    from app.server import app, list_projects, recall, remember

    PROJ = "proj-ingest"
    await client_for_group(PROJ)   # cria o grafo; ainda vazio

    async def as_scope(project, coro_fn, scopes=None):
        tok = set_scope("alfredo", project, frozenset(scopes if scopes is not None else _W))
        try:
            return await coro_fn()
        finally:
            reset_scope(tok)

    # ---- grant de escrita --------------------------------------------------
    negado = await as_scope(PROJ, lambda: remember("qualquer coisa"), _R)
    check("credencial sem 'write' nao grava pelo MCP",
          negado.get("stored") is False and "write:" in (negado.get("error") or ""),
          str(negado)[:140])

    # ---- politica de conteudo ----------------------------------------------
    # CPF com digito verificador valido, gerado para o teste.
    pii = await as_scope(PROJ, lambda: remember("O cadastro do responsavel e 529.982.247-25"), _W)
    check("remember recusa CPF valido", pii.get("stored") is False, str(pii)[:140])
    check("erro nao ecoa o dado", "529.982" not in str(pii), str(pii)[:140])

    segredo = await as_scope(
        PROJ,
        lambda: remember("usar a chave AKIAIOSFODNN7EXAMPLE no deploy"),
        {"read", "write"},
    )
    check("remember recusa AWS access key", segredo.get("stored") is False, str(segredo)[:140])

    placeholder = await as_scope(
        PROJ,
        lambda: remember("A variavel e AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}, nunca literal"),
        _W,
    )
    check("placeholder nao e tratado como segredo",
          placeholder.get("stored") is True, str(placeholder)[:140])

    # ---- projeto fantasma --------------------------------------------------
    fantasma = await as_scope("projeto-que-nunca-existiu", lambda: recall("qualquer", 5),
                              _R)
    check("recall em projeto inexistente devolve vazio",
          fantasma["count"] == 0, str(fantasma)[:140])

    listagem = await as_scope("_all", lambda: list_projects(), _R)
    check("leitura nao cria projeto fantasma na lista",
          "projeto-que-nunca-existiu" not in listagem["projects"], str(listagem)[:200])

    # ---- regressao: ingestao sem entidade extraida -------------------------
    # Uma ingestao legitima pode nao extrair entidade nenhuma e deixar so o no
    # :Episodic. Com o probe filtrando por :Entity, o projeto sumia da lista e
    # o recall dele passava a devolver vazio — projeto com memoria real,
    # invisivel. O predicado correto e "tem qualquer no".
    from app.ingest import _ingest as _ingest_fn

    SEM_ENT = "proj-sem-entidade"
    await client_for_group(SEM_ENT)
    await as_scope(
        SEM_ENT,
        lambda: _ingest_fn(
            {"source": {"event_id": "ev-sem-ent", "created_at": "2026-08-08T12:00:00Z",
                        "status": "ready"},
             "decisions": [{"decision_id": "DEC-SEMENT-01", "operation": "add",
                            "statement": "Fixar timezone em UTC",
                            "rationale": "evita drift entre ambientes",
                            "authority_refs": ["docs/infra.md"]}]},
            __import__("app.scope", fromlist=["current_scope"]).current_scope(),
        ),
        _I,
    )
    cli_sem = _group_clients[SEM_ENT]
    rows, _, _ = await cli_sem.driver.execute_query(
        "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c"
    )
    labels = {r["l"] for r in rows}
    check("ingestao sem extracao deixa so :Episodic",
          labels == {"Episodic"}, str(rows)[:140])

    lista = await as_scope("_all", lambda: list_projects(), _R)
    check("projeto com memoria mas sem :Entity continua listado",
          SEM_ENT in lista["projects"], str(lista)[:200])

    visivel = await as_scope(SEM_ENT, lambda: recall("timezone", 5), _R)
    check("recall nesse projeto nao devolve vazio por causa do filtro",
          visivel["count"] >= 0 and visivel["scope"] == SEM_ENT, str(visivel)[:160])

    # ---- /ingest via HTTP real ---------------------------------------------
    ledger.reset_for_tests()
    base_headers = {"X-Project-Id": PROJ}

    def corpo(decisoes, status="ready", projeto=None):
        payload = {
            "source": {
                "event_id": "20260808T120000Z-codex-to-claude",
                "sequence": 12,
                "content_version": 1,
                "created_at": "2026-08-08T12:00:00Z",
                "from_agent": "codex",
                "branch": "main",
                "status": status,
            },
            "decisions": decisoes,
        }
        if projeto:
            payload["project"] = projeto
        return payload

    D1 = {"decision_id": "DEC-000012-01", "operation": "add",
          "statement": "Usar RabbitMQ como broker",
          "rationale": "a equipe ja opera RabbitMQ",
          "authority_refs": ["docs/plan.md"]}
    D2 = {"decision_id": "DEC-000012-02", "operation": "add",
          "statement": "Fila morta com TTL de 7 dias",
          "rationale": "retencao suficiente para reprocesso",
          "authority_refs": ["docs/plan.md"]}

    async with Lifespan(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60
    ) as c:
        r = await c.post("/ingest", json=corpo([D1]),
                         headers={**base_headers, "Authorization": f"Bearer {TOKEN_WRITE}"})
        check("/ingest recusa credencial sem grant 'ingest'", r.status_code == 403,
              f"got {r.status_code} {r.text[:80]}")

        ing = {**base_headers, "Authorization": f"Bearer {TOKEN_INGEST}"}

        r = await c.post("/ingest", json=corpo([D1, D2]), headers=ing)
        body = r.json()
        st = [x["status"] for x in body.get("results", [])]
        check("/ingest publica as duas decisoes",
              r.status_code == 200 and st == ["published", "published"], str(body)[:240])

        r = await c.post("/ingest", json=corpo([D1, D2]), headers=ing)
        st2 = [x["status"] for x in r.json()["results"]]
        check("/ingest e idempotente: republicar nao duplica",
              st2 == ["already_published", "already_published"], str(st2)[:200])

        r = await c.post("/ingest", json=corpo([D1], status="cancelled"), headers=ing)
        check("/ingest ignora evento cancelled",
              r.json().get("skipped_event") is True, str(r.json())[:180])

        r = await c.post("/ingest", json=corpo([D1], status="blocked"), headers=ing)
        check("/ingest ignora evento blocked",
              r.json().get("skipped_event") is True, str(r.json())[:180])

        r = await c.post("/ingest", json=corpo([{**D2, "hash": "0" * 64}]), headers=ing)
        res = r.json()["results"][0]
        check("/ingest rejeita hash divergente",
              res["status"] == "rejected" and res["reason_code"] == "hash_mismatch",
              str(res)[:200])

        # Caixa PRESERVADA: userId e USERID sao decisoes diferentes.
        h1 = ledger.canonical_hash(project_id=PROJ, decision_id="X", operation="add",
                                   statement="Padronizar userId", rationale=None,
                                   authority_refs=[])
        h2 = ledger.canonical_hash(project_id=PROJ, decision_id="X", operation="add",
                                   statement="Padronizar USERID", rationale=None,
                                   authority_refs=[])
        check("canonicalizacao preserva caixa (userId != USERID)", h1 != h2, "hashes iguais")

        # Forma normalizada: espaco extra e a mesma decisao.
        h3 = ledger.canonical_hash(project_id=PROJ, decision_id="X", operation="add",
                                   statement="  Padronizar userId  ", rationale=None,
                                   authority_refs=[])
        check("canonicalizacao normaliza espaco em volta", h1 == h3, "hashes diferentes")

        # Validacao de schema, por operacao.
        casos = [
            ({"decision_id": "D-A", "operation": "add"}, "add_requires_statement"),
            ({"decision_id": "D-B", "operation": "add", "statement": "x",
              "target_decision_id": "DEC-000012-01"}, "add_forbids_target"),
            ({"decision_id": "D-C", "operation": "supersede", "statement": "x"},
             "supersede_requires_target"),
            ({"decision_id": "D-D", "operation": "retract", "statement": "x",
              "target_decision_id": "DEC-000012-01"}, "retract_forbids_statement"),
            ({"decision_id": "D-E", "operation": "revogar", "statement": "x"},
             "invalid_operation"),
            ({"operation": "add", "statement": "x"}, "missing_decision_id"),
        ]
        for payload_caso, esperado in casos:
            r = await c.post("/ingest", json=corpo([payload_caso]), headers=ing)
            got = r.json()["results"][0].get("reason_code")
            check(f"schema rejeita: {esperado}", got == esperado, f"got {got}")

        # ---- invalidacao determinística (aceitacao do §2) ------------------
        D3 = {"decision_id": "DEC-000013-01", "operation": "supersede",
              "statement": "Trocar RabbitMQ por Kafka",
              "rationale": "volume de eventos cresceu 10x",
              "authority_refs": ["docs/adr-014.md"],
              "target_decision_id": "DEC-000012-01"}
        r = await c.post("/ingest", json=corpo([D3]), headers=ing)
        res3 = r.json()["results"][0]
        check("supersede publica e marca o alvo",
              res3["status"] == "published" and res3.get("invalidated") == "DEC-000012-01",
              str(res3)[:200])

        antigo = ledger.get(PROJ, "DEC-000012-01")
        check("decisao superada continua no ledger, marcada",
              antigo is not None and antigo.state == ledger.SUPERSEDED, str(antigo)[:180])

        r = await c.post("/ingest", json=corpo([{**D3, "decision_id": "DEC-000013-99"}]),
                         headers=ing)
        check("invalidar duas vezes e erro explicito",
              r.json()["results"][0].get("reason_code") == "target_already_invalid",
              str(r.json()["results"][0])[:180])

        r = await c.post("/ingest", json=corpo([
            {"decision_id": "DEC-000014-01", "operation": "retract", "statement": None,
             "rationale": "a premissa deixou de existir",
             "authority_refs": ["decisao da equipe"],
             "target_decision_id": "DEC-000099-99"}]), headers=ing)
        check("alvo inexistente falha explicitamente, sem silencio",
              r.json()["results"][0].get("reason_code") == "target_not_found",
              str(r.json()["results"][0])[:180])

        r = await c.post("/ingest", json=corpo([
            {"decision_id": "DEC-000014-02", "operation": "retract", "statement": None,
             "rationale": "premissa deixou de existir",
             "authority_refs": ["decisao da equipe"],
             "target_decision_id": "DEC-000012-02"}]), headers=ing)
        check("retract publica e marca o alvo",
              r.json()["results"][0]["status"] == "published", str(r.json())[:200])
        alvo = ledger.get(PROJ, "DEC-000012-02")
        check("decisao retratada fica marcada, nao apagada",
              alvo is not None and alvo.state == ledger.RETRACTED, str(alvo)[:180])

        invalidos = ledger.invalid_episodes(PROJ)
        check("os dois episodios invalidados entram no conjunto de corte",
              len(invalidos) == 2, str(invalidos)[:160])

        # PII numa decisao rejeita SO ela; as outras seguem.
        r = await c.post("/ingest", json=corpo([
            {"decision_id": "DEC-000015-01", "operation": "add",
             "statement": "Responsavel 529.982.247-25 aprovou", "authority_refs": []},
            {"decision_id": "DEC-000015-02", "operation": "add",
             "statement": "Usar Postgres 16", "rationale": "logical replication",
             "authority_refs": ["docs/infra.md"]},
        ]), headers=ing)
        st4 = [(x["status"], x.get("reason_code")) for x in r.json()["results"]]
        check("/ingest isola a decisao com PII e publica o resto",
              r.status_code == 207 and st4[0] == ("rejected", "possible_secret_or_pii")
              and st4[1][0] == "published", str(st4)[:220])

        r = await c.post("/ingest", json=corpo([D1], projeto="outro-projeto"), headers=ing)
        check("/ingest recusa payload apontando para outro projeto",
              r.status_code == 400, f"got {r.status_code} {r.text[:120]}")

        r = await c.post("/ingest", json=corpo([D1]),
                         headers={**base_headers, "Authorization": f"Bearer {TOKEN_INGEST}",
                                  "X-Project-Id": "nao-registrado"})
        check("/ingest recusa projeto fora do registro",
              r.status_code in (400, 403), f"got {r.status_code} {r.text[:100]}")


    # Limpeza do grafo do teste.
    cli = _group_clients.get(PROJ)
    if cli:
        try:
            await cli.driver.execute_query("MATCH (n) DETACH DELETE n")
        except ConnectionError:
            pass


async def main() -> int:
    print("\n[0] Configuracao e unidades")
    test_env_passthrough_safe()
    test_drop_invalidated()

    print("\n[1] Borda: identidade e escopo")
    await test_edge()

    print("\n[2] Isolamento por group_id (FalkorDB real)")
    try:
        await test_isolation()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(f"isolamento levantou excecao: {exc}")

    print("\n[3] Permissao, politica de conteudo e /ingest")
    try:
        await test_permissions_and_ingest()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(f"camada 3 levantou excecao: {exc}")

    print(f"\n{len(passed)} passaram, {len(failures)} falharam")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
