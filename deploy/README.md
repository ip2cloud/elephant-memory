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
printf '%s' 'sk-...'            | docker secret create elephant_openai_key -
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

O nome do service token vira o claim `common_name` do JWT — é a chave do
`CF_ACCESS_GRANTS`. Nome do token e chave do mapa **têm que bater**.

Service tokens expiram (padrão até 1 ano). Quando vencerem, tudo para junto —
vale lembrete no calendário.

## 4. Variáveis no Portainer

```
GHCR_OWNER             ip2cloud
ELEPHANT_TAG           0.1.0
CF_ACCESS_TEAM_DOMAIN  seutime.cloudflareaccess.com
ELEPHANT_CF_AUD        <AUD da Access App>
PROJECTS               ["scout-manager"]
CF_ACCESS_GRANTS       {"repo-scout":["read:scout-manager"],"alfredo-pessoal":["read:scout-manager","write:scout-manager"],"publicador-scout":["read:scout-manager","ingest:scout-manager"]}
```

O boot **recusa subir** se algum grant apontar para projeto fora de `PROJECTS`.
É proposital: typo em permissão vira erro de inicialização, não descoberta
tardia.

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
