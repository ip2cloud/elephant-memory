"""Smoke test com ingestao REAL — o unico que exercita LLM e embeddings.

A suite de isolamento semeia com `add_triplet` e stuba o LLM: prova isolamento,
permissao e ledger, mas nao o caminho de producao (`add_episode` + extracao).
Este script fecha essa lacuna.

Rode:

    export OPENAI_API_KEY=sk-...
    export FALKORDB_HOST=127.0.0.1
    python3 tests/smoke.py

Custa alguns centavos de LLM e leva ~1 minuto.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FALKORDB_DATABASE", "memoria_smoke")

A = "smoke-projeto-a"
B = "smoke-projeto-b"

# O registro e a autoridade sobre o que existe, e permissao e por projeto.
os.environ.setdefault("PROJECTS", f'["{A}","{B}"]')
GRANTS = frozenset({f"read:{A}", f"read:{B}", f"write:{A}", f"write:{B}",
                    f"ingest:{A}", f"ingest:{B}"})

ok, bad = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FALHOU'}  {name}{(' :: ' + detail) if detail else ''}")


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY", "").startswith("sk-"):
        print("Defina OPENAI_API_KEY de verdade. Este teste faz chamadas reais.")
        return 2

    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType
    from app import graph_store

    client = Graphiti(graph_driver=graph_store.build_driver())
    await client.build_indices_and_constraints()
    await graph_store.set_client(client)
    await client.driver.execute_query("MATCH (n) DETACH DELETE n")

    print("\n[1] Ingestao real (add_episode + extracao por LLM)")
    for group, texto in (
        (A, "No projeto A decidimos usar RabbitMQ como broker de mensageria, "
            "porque a equipe ja opera RabbitMQ em producao."),
        (B, "No projeto B decidimos usar Kafka como broker de mensageria, "
            "por causa do volume de eventos por segundo."),
    ):
        await client.add_episode(
            name="decisao de mensageria",
            episode_body=texto,
            source=EpisodeType.text,
            source_description="smoke",
            reference_time=datetime.now(timezone.utc),
            group_id=group,
        )
        print(f"  gravado em {group}")

    print("\n[2] Busca COM filtro de projeto — o ponto em duvida")
    from app.scope import reset_scope, set_scope
    from app.server import recall

    async def as_scope(project, fn):
        tok = set_scope("smoke", project, GRANTS)
        try:
            return await fn()
        finally:
            reset_scope(tok)

    ra = await as_scope(A, lambda: recall("qual broker de mensageria?", 10))
    fa = " ".join((f.get("fact") or "") for f in ra["facts"])
    check("projeto A retorna fato proprio (NAO pode vir vazio)", ra["count"] > 0, f"count={ra['count']}")
    check("projeto A menciona RabbitMQ", "RabbitMQ" in fa, fa[:140])
    check("projeto A nao vaza Kafka", "Kafka" not in fa, fa[:140])

    rb = await as_scope(B, lambda: recall("qual broker de mensageria?", 10))
    fb = " ".join((f.get("fact") or "") for f in rb["facts"])
    check("projeto B retorna fato proprio", rb["count"] > 0, f"count={rb['count']}")
    check("projeto B menciona Kafka", "Kafka" in fb, fb[:140])
    check("projeto B nao vaza RabbitMQ", "RabbitMQ" not in fb, fb[:140])

    print("\n[3] Modo _all (cenario Cowork)")
    todos = await as_scope("_all", lambda: recall("qual broker de mensageria?", 20))
    ft = " ".join((f.get("fact") or "") for f in todos["facts"])
    check("_all ve os dois projetos", "RabbitMQ" in ft and "Kafka" in ft, ft[:200])

    from app.server import recall_project

    # Frase, nao termo isolado: esta asserção mede ESCOPO. A sensibilidade a
    # termo unico e uma limitacao conhecida da busca hibrida e tem teste proprio
    # abaixo — misturar as duas fazia esta falhar acusando o culpado errado.
    nomeado = await as_scope("_all", lambda: recall_project("qual broker de mensageria?", B, 10))
    fn_ = " ".join((f.get("fact") or "") for f in nomeado.get("facts", []))
    check("_all consulta projeto nomeado", "Kafka" in fn_, str(nomeado)[:200])

    print("\n[4] Limitacao conhecida: query de termo unico")
    curto = await as_scope(B, lambda: recall("broker", 10))
    frase = await as_scope(B, lambda: recall("qual broker de mensageria?", 10))
    print(f"  termo unico 'broker'          -> {curto['count']} fato(s)")
    print(f"  frase 'qual broker de ...?'   -> {frase['count']} fato(s)")
    if curto["count"] == 0 and frase["count"] > 0:
        print("  DOCUMENTADO: termo isolado nao casa com fato longo. A busca "
              "hibrida corta por similaridade, e um vetor de uma palavra contra "
              "uma sentenca fica abaixo do limiar. Por isso a descricao da tool "
              "`recall` instrui consulta por frase.")
    check("frase sempre acha (a garantia que o produto oferece)", frase["count"] > 0,
          str(frase)[:140])

    await client.driver.execute_query("MATCH (n) DETACH DELETE n")
    await client.close()

    print(f"\n{len(ok)} passaram, {len(bad)} falharam")
    for f in bad:
        print(f"  - {f}")
    if bad:
        print(
            "\nSe 'retorna fato proprio' falhou, NAO construa em cima disto ainda: "
            "significa que a busca com escopo de projeto nao esta achando nada, "
            "e todo o desenho depende disso. Veja 'Pendencia aberta' no README."
        )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
