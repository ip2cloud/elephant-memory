# Memória em grafo self-hosted (Graphiti) com isolamento por projeto

MCP server de memória temporal em grafo. **Um servidor para todos os projetos**,
separação automática entre eles, atrás do Cloudflare Access. Claude Code, Codex
e Cowork.

> **Estado:** `tests/test_isolation.py` passa **29/29** contra FalkorDB real.
> `tests/smoke.py` (ingestão real com LLM) valida o caminho de produção.
> A "pendência aberta" de versões anteriores deste README **foi resolvida** —
> era artefato do harness. Ver *Um grafo por projeto* abaixo.

---

## Desenho

```
Claude Code / Codex / Cowork
   CF-Access-Client-Id / -Secret   →  Cloudflare Access autentica
   X-Project-Id  ou  /p/<projeto>  →  group_id do Graphiti
                    ↓ HTTPS
   Cloudflare Tunnel  (sem porta aberta, sem IP público)
                    ↓
   MCP server ── ScopeMiddleware (valida o JWT do Access, fixa o escopo)
                    ↓
   Graphiti ── FalkorDB (rede interna)
```

O projeto **não** vem do modelo. Chega no transporte e é fixado num `ContextVar`
antes de qualquer tool rodar.

## Subir

```bash
cp .env.example .env && $EDITOR .env
docker compose up -d --build
```

Para rodar local, sem Cloudflare no caminho (publica em `127.0.0.1:8088`):

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

No Cloudflare Zero Trust: crie o Tunnel (cole o token no `.env`), publique o
serviço apontando para `http://mcp:8080`, e crie uma Access Application com
policy **Service Auth** + service token. O `CF_ACCESS_AUD` está em
Access → Applications → sua app → Overview.

## Ligar os projetos

**Claude Code — um registro só, para todos os repos:**

```bash
cp scripts/mcp-project-headers ~/.local/bin/ && chmod +x ~/.local/bin/mcp-project-headers
claude mcp add --transport http memoria --scope user https://memoria.SEU.DOMINIO/mcp
# em ~/.claude.json, troque "headers" por:
#   "headersHelper": "~/.local/bin/mcp-project-headers"
```

O helper roda no diretório do projeto a cada conexão, deriva o slug do git
remote e emite o `X-Project-Id` junto com as credenciais. **Zero arquivo por
repo.** Confirme na sua versão que ele roda com o cwd do projeto — é um teste de
trinta segundos e decide se você usa este caminho ou o de baixo.

**Codex — não tem equivalente, então é por repo:**

```bash
# local, contra o stack na sua maquina
MEM_MODE=token MEM_URL=http://127.0.0.1:8088/mcp \
  ./scripts/setup-project.sh ~/code/scout-manager

# producao, atras do Cloudflare Access
MEM_MODE=cloudflare MEM_URL=https://memoria.SEU.DOMINIO/mcp \
  ./scripts/setup-project.sh ~/code/scout-manager
```

Gera `.mcp.json` e `.codex/config.toml`. Sem segredo dentro — podem ir pro git.

**Cowork** — ver a seção abaixo.

## O caso do Cowork: escopo `_all`

O Cowork não tem um projeto por sessão. Para ele existe o escopo reservado
`_all`, com uma assimetria deliberada:

| | em `_all` |
|---|---|
| **Leitura** | varre todos os projetos, cada fato rotulado com o seu |
| **Escrita** | **recusada** sem `project` explícito e existente |

Leitura errada devolve resultado irrelevante. Escrita errada contamina um
projeto por semanas. Por isso só uma das duas é livre.

"Sobre o projeto X, o que a gente decidiu?" funciona via `recall_project`, que
valida o nome contra os projetos existentes e devolve **erro com a lista** se
não achar — nunca uma lista vazia, que se confunde com "não há nada gravado".

Se o Cowork não deixar setar header, use o escopo por caminho:
`https://memoria.SEU.DOMINIO/p/_all/mcp`.

## Ferramentas

| Tool | O que faz | Grant |
|---|---|---|
| `remember` | grava um episódio (extração por LLM) | **`write:<projeto>`** |
| `recall` | fatos do projeto atual — ou de todos, em `_all` | `read:<projeto>` |
| `recall_project` | fatos de um projeto nomeado | `read:<projeto>` |
| `recall_other_projects` | fatos dos outros projetos — a escotilha do isolamento | `read:<projeto>` |
| `search_entities` | nós do grafo, não fatos | `read:<projeto>` |
| `list_projects` | projetos com memória | `read:<projeto>` |

Os fatos vêm com `valid_at` e `invalid_at`. `invalid_at` preenchido significa
"isto **já foi** verdade e deixou de ser" — é a parte bitemporal, o que
diferencia isto de uma busca vetorial.

## Registro de projetos

`PROJECTS` é a **autoridade** sobre o que existe. ID fora do registro falha, sem
criar grafo. Criar projeto é ato administrativo: editar a variável e reiniciar —
nenhuma credencial cria projeto escrevendo.

```
list_projects = projetos registrados ∩ projetos com read: nesta credencial
```

Projeto registrado e vazio continua visível; grafo criado por acidente nunca
vira projeto. O boot **recusa subir** se um grant apontar para projeto fora do
registro — typo em permissão vira erro de inicialização, não descoberta tardia.

## Vigência: o ledger decide, não o LLM

`supersede` e `retract` invalidam no ledger, e o `recall` **não devolve** fatos
cujas únicas origens foram invalidadas. O Graphiti guarda o histórico e as
relações temporais; o ledger diz o que ainda vale.

```
active --supersede--> superseded
active --retract----> retracted
```

Nada é apagado. Invalidar duas vezes, ou apontar para um `decision_id`
inexistente, é erro explícito — não silêncio.

## Contrato da decisão

```yaml
decision_id: "DEC-000012-01"      # único e imutável no projeto
operation: add                     # add | supersede | retract
statement: "Usar RabbitMQ como broker"
rationale: "a equipe já opera RabbitMQ"
authority_refs: ["docs/plan.md"]
target_decision_id: null           # exigido em supersede e retract
```

`add` exige `statement` e proíbe `target_decision_id`. `supersede` exige os
dois. `retract` exige `target_decision_id` e proíbe `statement`.

O hash canônico (`canonicalization_version: 1`) normaliza **forma** — NFC,
chaves ordenadas, sem espaço insignificante — e **preserva caixa e conteúdo**:
`userId` e `USERID` são decisões diferentes.

## Duas portas de escrita

| Porta | Para quem | Grant |
|---|---|---|
| `POST /ingest` | projetos com `.ia/` — publicação determinística pelo handoff | `ingest` |
| MCP `remember` | projetos sem `.ia/` e uso manual no Cowork | `write` |

**Escrever é permissão da credencial, não propriedade do servidor.** O token que
vai versionado no `.mcp.json` de um repo tem só `read`; o pessoal tem `write`;
o do publicador tem `ingest` e nem precisa falar MCP. Uma instrução dizendo
"não escreva" não é controle de segurança — um grant negado é.

`AUTH_TOKENS` na forma curta (`{"tok_...": "alfredo"}`) concede **apenas
leitura**, de propósito.

### Publicador do handoff

`scripts/publish-memory` → copie para `.ia/bin/publish-memory`. Lê o evento
apontado por `index.yaml`, extrai `decisions` do **frontmatter** (tabela
markdown é deliberadamente não suportada), e publica. Falha é não-bloqueante.

```
9. Run `.ia/bin/publish-memory`. Failure is non-blocking.
```

O ledger (`/data/ledger.db`, em volume) guarda `(group_id, decision_hash)`.
Republicar o mesmo handoff não duplica: o hash é canonicalizado (NFC, espaços,
caixa), então variação de formatação é o mesmo fato. `operation: supersede`
grava tombstone sem apagar histórico.

O `uuid` do episódio vem do Graphiti, não do hash: `add_episode(uuid=X)`
**busca** um episódio existente, não cria um com aquele id.

### Barreira de conteúdo

Segredos (chaves AWS/GitHub/OpenAI/Stripe/Slack, JWT, chave privada, senha em
connection string) e PII com dígito verificador (CPF, CNPJ, cartão via Luhn)
**bloqueiam a gravação** nas duas portas. Placeholder (`${VAR}`, `<seu-token>`,
`EXAMPLE`) não conta. Nada é sanitizado em silêncio, e o erro nunca ecoa o valor.

Isso é bloqueante porque o texto vai para um LLM externo na extração: gravar e
vazar são o mesmo evento.

## Testes

O FalkorDB só existe na rede interna, então os testes rodam dentro do stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml run --rm --no-deps \
  -v $PWD/tests:/srv/tests:ro \
  -e FALKORDB_HOST=falkordb -e FALKORDB_DATABASE=memoria_smoke \
  mcp python tests/smoke.py       # ingestão real — precisa de OPENAI_API_KEY

# isolamento, sem LLM. LEDGER_DB precisa ir junto, senao a suite herda o
# /data/ledger.db do compose (que so o usuario do container consegue abrir).
LEDGER_DB=/tmp/graphmem-test-ledger.db python3 tests/test_isolation.py
```

A suíte define os próprios tokens (leitura, escrita e ingest). Não force
`AUTH_TOKENS` por `-e` ao rodar dentro do container: sobrescrever com um token
só faz todo o bloco de permissões e `/ingest` falhar.

**66 asserções, todas passando.**

*Borda:* healthz sem auth → 200; sem credencial → 401; credencial inválida →
401; sem projeto → 400; `X-Project-Id` malicioso (`../outro`, `PROJ/../..`,
espaço, 80 chars, vazio) → 400; escopo por caminho aceito; caminho malicioso
rejeitado.

*Isolamento:* A acha o próprio fato e **não** vê o de B (e vice-versa); todo
fato carrega o projeto correto; `recall_other_projects` alcança o outro e exclui
o próprio; projeto com hífen (`acme-api-billing`) acha o próprio fato.

*Modo `_all`:* vê os dois projetos; consulta projeto nomeado; não vaza o outro;
projeto inexistente devolve **erro com a lista**, nunca vazio; recusa escrita sem
projeto; recusa escrita em projeto inexistente; conexão presa recusa gravar em
outro projeto.

*Regressão do grafo-por-projeto:* `list_projects` é determinístico depois de
tocar projetos diferentes; `_all` declara os projetos varridos.

*Permissão e conteúdo:* credencial sem `write` não grava pelo MCP; `remember`
recusa CPF válido e chave AWS, sem ecoar o valor; placeholder não é tratado como
segredo; leitura em projeto inexistente devolve vazio e **não cria fantasma**
na lista.

*Configuração:* toda variável lida pelo app é repassada no compose. Esta é a
única asserção que não precisa de banco, e existe porque a classe de bug que ela
pega é invisível para as outras: as duas suítes rodam em `AUTH_MODE=token`, então
uma variável do caminho Cloudflare pode faltar no compose sem nada falhar — e o
efeito seria o servidor subir somente-leitura para todo mundo, em produção.

*Probe de projeto:* ingestão sem extração de entidade deixa só `:Episodic`, e o
projeto **continua listado e legível** — o predicado é "tem qualquer nó", não
"tem nó `:Entity`". Filtrar por `:Entity` fazia projeto com memória real sumir.

*`/ingest`:* recusa credencial sem grant; publica; é idempotente ao republicar;
ignora evento `cancelled` e `blocked`; rejeita hash divergente; trata variação
de espaço/caixa como o mesmo fato; grava tombstone mantendo o histórico; isola a
decisão com PII e publica as demais; recusa payload apontando para outro projeto.

Além disso, round-trip real por socket: `publish-memory` → HTTP → middleware →
`/ingest` → ledger, com republicação devolvendo tudo como já publicado.

O harness semeia com `add_triplet` **no grafo de cada projeto** — não no grafo
default. Semear no default foi o que produziu as falhas fantasma de versões
anteriores: gravava num grafo e buscava em outro.

## Um grafo por projeto — o detalhe que muda tudo

No FalkorDB, o Graphiti **não** guarda tudo num grafo com propriedade
`group_id`. Ele cria **um grafo do FalkorDB por `group_id`** (ver
`FalkorDriver.clone` e o decorator de multi-tenancy em `graphiti_core`). O grafo
configurado em `FALKORDB_DATABASE` fica vazio.

Isso é bom: o isolamento é **físico**, não um `WHERE`. Mas quebra duas coisas se
você não souber:

**1. Enumerar projetos por Cypher não funciona — e ainda contamina a query
seguinte.** `driver.execute_query()` sempre mira `self._database`, e o graphiti
troca esse campo conforme o último projeto tocado. Sintoma observado:
`list_projects` devolvendo ora um projeto, ora outro; e `recall_project`
retornando `count: 0` para um projeto que ele *acabou de listar* — porque a
chamada de enumeração moveu o driver para o grafo errado antes da busca.

A fonte de verdade é `GRAPH.LIST`, via a conexão Redis. Não passa por
`select_graph`, então não mexe no `_database` e não contamina nada.

**2. `group_ids=None` varre só o grafo corrente.** O Graphiti só faz fan-out
entre grafos quando recebe a lista. Em modo `_all`, passar `None` devolve um
resultado silenciosamente parcial. Por isso `_scope_groups()` sempre resolve a
lista explícita, e `recall` devolve `projects_searched` para você conferir.

Ambos estão corrigidos e cobertos por teste de regressão (`list_projects é
determinístico após tocar projetos diferentes`, `_all declara os projetos
varridos`).

**Sobre o hífen:** o escape que o Graphiti gera (`@group_id:"proj\-alpha"`)
devolve vazio no FalkorDB 4.18.3 *embutido* que usei, mas **não reproduz** na
imagem oficial — validado com ingestão real em `acme-api-billing` e
`smoke-projeto-a`. Não há mitigação a fazer; pode usar slug de git remote
normalmente.

## A imagem do FalkorDB ignora `command:`

`falkordb/falkordb` tem ENTRYPOINT próprio (`run.sh`) que monta a linha do
`redis-server` a partir de `REDIS_ARGS` / `FALKORDB_ARGS`. Um `command:` no
compose é **descartado em silêncio**. Três consequências de uma vez:

- o banco sobe **sem senha** (`--requirepass` nunca aplicado)
- **nada persiste** — o volume vai em `/data`, mas o `run.sh` usa
  `--dir /var/lib/falkordb/data`
- o healthcheck fica **verde assim mesmo**: num servidor aberto o AUTH falha mas
  o PING responde `PONG`

O compose deste repo usa `REDIS_ARGS`, monta o volume no caminho certo, desliga
a UI Next.js da porta 3000, e o healthcheck afirma as duas coisas — que responde
**e** que exige senha:

```
redis-cli ping | grep -q NOAUTH && redis-cli -a "$PASS" ping | grep -q PONG
```

## Armadilhas de dependência

**`redis` só funciona no 8.0.x.** No 8.1.0 o cliente falkordb quebra com
`TypeError: Redis.__init__() got an unexpected keyword argument 'himport_registry'`.
Abaixo de 8, quebra com `ModuleNotFoundError: redis.driver_info`. A janela é
estreita — não afrouxe o pin sem rodar os testes.

**`mcp` 2.0.0 removeu `mcp.server.fastmcp`.** Daí o pin em 1.29.0.

**Stubs de teste precisam herdar de `EmbedderClient`/`CrossEncoderClient`.** O
Graphiti valida os clientes com pydantic (`is_instance_of`); duck typing não passa.

**Service tokens do Access expiram** (padrão até 1 ano). Quando vencer, todos os
projetos param juntos. Coloque lembrete.

**O Cloudflare rotaciona as chaves de assinatura a cada ~6 semanas** (antigas
valem 7 dias). O JWKS é buscado com cache de 1h — nunca embuta as chaves.

## Custo

A ingestão do Graphiti é mais cara que a de um vector store: além de extrair
fatos, resolve entidades, relações e invalidação temporal. São várias chamadas
de LLM por `remember`. Em troca você tem multi-hop e bitemporalidade — que é o
motivo de ter escolhido grafo.
