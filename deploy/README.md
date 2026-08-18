# Deploy na infra IP2 (Docker Swarm)

Segue o padrão do `stack-ouro-mcp.yml`: imagem no GHCR, redes externas, secrets
do Swarm com convenção `*_FILE`, exposição 100% via Cloudflare Tunnel — sem
porta publicada, sem label de Traefik.

Repositório: `git@github.com:ip2cloud/elephant-memory.git`

## Bloqueio atual

**A imagem não existe no registry.** O stack referencia
`ghcr.io/ip2cloud/elephant-memory:${ELEPHANT_TAG}`; sem ela, o deploy falha em
`image pull`. O passo 1 resolve.

## 1. Publicar a imagem

Pelo CI (`.github/workflows/publish.yml`):

```bash
git tag v0.1.0 && git push origin v0.1.0
```

O workflow roda a validação de configuração, valida o YAML do stack, builda
para `linux/amd64` e publica. O **digest** sai no summary do job — é ele que vai
no registro de evidência, não a tag: tag pode ser movida, digest não.

Na mão, se preferir (Mac ARM contra Swarm x86 exige o `--platform`):

```bash
docker buildx build --platform linux/amd64 \
  -t ghcr.io/ip2cloud/elephant-memory:0.1.0 --push .
```

Não existe `latest`, de propósito. O rebuild do grafo depende de versão fixa:
trocar a imagem muda o resultado da extração a partir dos mesmos eventos, e
`latest` apagaria a rastreabilidade do que gerou o grafo que está no ar.

## 2. Recursos externos, uma vez

As redes já existem (`network_swarm_public`, `network_swarm_databases`). Faltam
os secrets:

```bash
printf '%s' 'sk-...'            | docker secret create elephant_openai_key_v3 -
openssl rand -base64 24 | tr -d '\n' | docker secret create elephant_falkordb_password -
```

`printf` em vez de `echo`: o newline do `echo` entra no secret e vira senha
errada sem nenhuma mensagem que explique.

## 3. Cloudflare Zero Trust

1. **Tunnel** → Public Hostname: `memoria.SEU.DOMINIO` → `http://elephant-mcp:8080`
2. **Access** → Application no mesmo hostname, policy **Service Auth**
3. Anote o **Application Audience (AUD)** → vai em `ELEPHANT_CF_AUD`
4. Crie os service tokens, um por papel:

| Token | Grants | Onde vive |
|---|---|---|
| `repo-scout` | `read:scout-manager` | `.mcp.json` do repositório |
| `alfredo-pessoal` | `read:` + `write:scout-manager` | Cowork, shell |
| `publicador-scout` | `read:` + `ingest:scout-manager` | ambiente do publisher |

A chave do `CF_ACCESS_GRANTS` é o **Client ID** do service token — a string
`<32 hex>.access`, não o apelido que você deu a ele. É isso que o Cloudflare põe
no claim `common_name`, e é o que `app/auth.py` procura no mapa. O apelido serve
só para você achar o token na tela do Zero Trust.

Errar aqui **não quebra o boot**: `assert_grants_within_registry` valida os
projetos, não as identidades. Uma chave com apelido em vez de Client ID casa com
ninguém, `grants` fica vazio, e o sintoma é "esta credencial não tem
`write:projeto`" numa credencial que autentica perfeitamente.

Service tokens expiram (padrão até 1 ano). Quando vencerem, tudo para junto —
vale lembrete no calendário. Se criar vários de uma vez, escalone as datas de
propósito: com todos vencendo no mesmo dia, o sintoma é `403` do Cloudflare em
tudo simultaneamente, o que não se parece com "token venceu".

## 4. Variáveis no Portainer

```
GHCR_OWNER             ip2cloud
ELEPHANT_TAG           0.1.1
MCP_ALLOWED_HOSTS      memoria.SEU.DOMINIO
CF_ACCESS_TEAM_DOMAIN  seutime.cloudflareaccess.com
ELEPHANT_CF_AUD        <AUD da Access App>
PROJECTS               ["scout-manager"]
CF_ACCESS_GRANTS       {"<clientid-repo>.access":["read:scout-manager"],"<clientid-pessoal>.access":["read:scout-manager","write:scout-manager"],"<clientid-publicador>.access":["read:scout-manager","ingest:scout-manager"]}
```

Cada `<clientid-*>` é o **Client ID** do service token correspondente
(`<32 hex>.access`), copiado de Zero Trust → Access → Service Auth. Ver a nota
no passo 3 sobre por que o apelido do token não funciona aqui.

O boot **recusa subir** se algum grant apontar para projeto fora de `PROJECTS`.
É proposital: typo em permissão vira erro de inicialização, não descoberta
tardia.

`MCP_ALLOWED_HOSTS` tem que ser exatamente o hostname do **Public Hostname do
Tunnel**, e o boot também recusa subir sem ela quando `AUTH_MODE=cloudflare`.
Motivo: o SDK do MCP liga proteção contra DNS rebinding sozinho quando o host do
`FastMCP` fica no default de loopback, e aí só aceita `Host` de loopback. Atrás
do Tunnel o Host que chega é o público, e **todo** request morria em
`421 Invalid Host header` — dentro do transporte, antes do middleware, sem uma
linha no log do container. Custou um deploy inteiro para aparecer; o teste
`Host publico declarado nao vira 421` existe para não custar um segundo.

## 5. Deploy

Portainer → Stacks → Add stack → **Repository**, no padrão do `ouro-mcp`:

```
Repository URL  git@github.com:ip2cloud/elephant-memory.git
Compose path    deploy/stack-elephant-memory.yml
Reference       refs/heads/main
Auto update     Webhook (push → redeploy)
```

Ou pela CLI:

```bash
docker stack deploy -c deploy/stack-elephant-memory.yml elephant-memory --with-registry-auth
```

## 6. Verificação, nesta ordem

```bash
# 1. healthz responde (é público por desenho — não expõe nada)
curl -s https://memoria.SEU.DOMINIO/healthz

# 2. SEM credencial deve dar 401 DO CLOUDFLARE, antes de chegar no container.
#    Se vier 401 do app, a policy do Access está frouxa e o Tunnel está
#    entregando tráfego não autenticado no origin.
curl -si https://memoria.SEU.DOMINIO/mcp | head -1

# 3. COM service token e sem projeto -> 400 do app
curl -si https://memoria.SEU.DOMINIO/mcp \
  -H "CF-Access-Client-Id: ...access" -H "CF-Access-Client-Secret: ..." | head -1

# 4. projeto fora do registro -> 400/403
curl -si https://memoria.SEU.DOMINIO/mcp \
  -H "CF-Access-Client-Id: ...access" -H "CF-Access-Client-Secret: ..." \
  -H "X-Project-Id: nao-registrado" | head -1
```

O passo 2 é o que importa. Os outros só confirmam que o app está vivo.

**`421` em qualquer passo a partir do 3** significa `MCP_ALLOWED_HOSTS` ausente
ou diferente do hostname do Tunnel — não é problema de credencial nem de
projeto. O request nem chegou no nosso código. A partir da 0.1.1 o serviço nem
sobe sem a variável, então `421` aqui quer dizer valor **errado**, não faltando.

## 7. FalkorDB Browser (opcional)

O serviço `elephant-browser` serve a UI de inspeção do grafo. É **opcional**: o
`redis-cli` dentro do container do FalkorDB resolve consulta pontual sem abrir
superfície nenhuma.

```bash
CID=$(docker ps -qf name=elephant-memory_falkordb)
Q() { docker exec -i $CID sh -c "redis-cli -a \"\$(cat /run/secrets/elephant_falkordb_password)\" --no-auth-warning $*"; }

Q GRAPH.LIST                                     # um grafo por projeto
Q 'GRAPH.QUERY jarvis "MATCH (n) RETURN labels(n), count(*)"'
```

Lembre que **existe um grafo do FalkorDB por projeto**, e que o grafo nomeado em
`FALKORDB_DATABASE` fica vazio — olhar só ele leva à conclusão errada de que não
há nada gravado.

### Por que ele é admin, não ferramenta de agente

O browser autentica no banco pela **senha do FalkorDB**, não por service token.
Consequência: enxerga e **escreve** em todos os projetos. O modelo
`read:`/`write:`/`ingest:` por projeto não se aplica a ele — Cypher aceita
`DELETE`, e `GRAPH.DELETE` derruba um projeto inteiro.

Por isso a Access Application dele é **separada**, com policy de **login humano**
(email + MFA). Nunca Service Auth, e os service tokens do MCP não devem constar
nessa policy. E note a assimetria com o `elephant-mcp`: aquele valida o JWT por
conta própria, então o Access é defesa em profundidade; aqui o Access é a
**única** barreira.

### Ligar

1. Variáveis no Portainer: `BROWSER_HOST` (hostname público, sem esquema) e
   `BROWSER_TAG` (ex: `v2.4.0` — não use `latest`)
2. Tunnel → Public Hostname: `${BROWSER_HOST}` → `http://elephant-browser:3000`
3. Access → Application nova nesse hostname, policy de login humano
4. Update the stack

Teste antes de expor, direto no manager:

```bash
docker run --rm -e FALKORDB_URL=redis://elephant-falkordb:6379 \
  -e FALKORDB_PASSWORD="$(docker exec $(docker ps -qf name=elephant-memory_falkordb) \
     cat /run/secrets/elephant_falkordb_password)" \
  --network network_swarm_databases -p 127.0.0.1:3000:3000 \
  falkordb/falkordb-browser:v2.4.0
```

Se ele reclamar de `NEXTAUTH_SECRET` no boot, crie um secret e injete pelo mesmo
entrypoint que já lê a senha — a imagem não conhece a convenção `_FILE`.

### Não ligue `BROWSER=1` no serviço `falkordb`

Aquele container está só em `network_swarm_databases`. Colocá-lo na rede pública
para expor a UI aproximaria o **banco** do túnel. No desenho atual só o browser
cruza as duas redes — mesma forma do `elephant-mcp`.

## Decisões deste stack

**FalkorDB dedicado, não o `neo4j` vizinho.** Aquele é Community Edition —
banco único — e já serve os fluxos do n8n. Misturar a memória dos agentes com
aquele grafo juntaria dois raios de dano sem ganho.

**Um serviço, não o par interno/público do `ouro-mcp`.** Toda a permissão aqui é
por credencial. Um gêmeo interno "que confia na rede" contornaria o modelo
inteiro de `read:`/`write:`/`ingest:` por projeto.

**`replicas: 1`, e não escale sem tratar dois estados:** o ledger é SQLite em
volume local, e o rate limiter é em memória por processo. Com duas réplicas, o
ledger diverge e o limite vira o dobro.

**`order: stop-first` no update.** `start-first` colocaria dois containers no
mesmo volume do ledger durante o rollout.

**Ambos os serviços fixados em `node.role == manager`.** Volume local: um
reagendamento para outro nó troca o volume por um vazio, e o grafo "some".

## Pendências antes de considerar produção

- registrar o digest da imagem publicada (sai no summary do CI)
- procedimento de restore do ledger e do volume do FalkorDB, ensaiado
- alerta de expiração dos service tokens
- varredura periódica de auditoria do grafo (segredo/PII que tenha passado)
