#!/usr/bin/env bash
# Gera as configs de MCP de UM repositorio (Claude Code + Codex).
#
#   # local, contra o stack rodando na sua maquina:
#   MEM_MODE=token MEM_URL=http://127.0.0.1:8088/mcp ./setup-project.sh ~/code/api-billing
#
#   # producao, atras do Cloudflare Access:
#   MEM_MODE=cloudflare MEM_URL=https://memoria.SEU.DOMINIO/mcp ./setup-project.sh ~/code/api-billing
#
# O project-id sai do git remote (git@github.com:Acme/API-Billing.git ->
# acme-api-billing), ou pode vir como segundo argumento.
#
# Nenhum segredo entra nos arquivos: so a REFERENCIA a variavel de ambiente.
# Pode commitar os dois.

set -euo pipefail

REPO="${1:?uso: setup-project.sh <repo> [project-id]}"
EXPLICIT="${2:-}"
MEM_MODE="${MEM_MODE:-cloudflare}"
: "${MEM_URL:?exporte MEM_URL (ex: https://memoria.seudominio.dev/mcp)}"

cd "$REPO"

if [[ -n "$EXPLICIT" ]]; then
	PROJECT_ID="$EXPLICIT"
elif remote=$(git remote get-url origin 2>/dev/null) && [[ -n "$remote" ]]; then
	slug="${remote%.git}"; slug="${slug#*://}"; slug="${slug#*@}"; slug="${slug#*[:/]}"
	slug="${slug//\//-}"; slug="${slug//:/-}"
	PROJECT_ID=$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]')
else
	PROJECT_ID=$(basename "$PWD" | tr '[:upper:]' '[:lower:]')
fi
PROJECT_ID=$(printf '%s' "$PROJECT_ID" | tr -cd 'a-z0-9._-')
[[ "$PROJECT_ID" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || {
	echo "project-id invalido apos normalizacao: '$PROJECT_ID'" >&2; exit 1; }

case "$MEM_MODE" in
token)
	CC_AUTH='        "Authorization": "Bearer ${MEM_TOKEN}",'
	CODEX_AUTH='bearer_token_env_var = "MEM_TOKEN"'
	CODEX_ENV_HEADERS=""
	VARS="export MEM_TOKEN=tok_..."
	;;
cloudflare)
	CC_AUTH='        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}",'
	CODEX_AUTH=""
	# Chave = nome do header. Valor = NOME DA VARIAVEL, nao o valor dela.
	CODEX_ENV_HEADERS='
[mcp_servers.memoria.env_http_headers]
CF-Access-Client-Id = "CF_ACCESS_CLIENT_ID"
CF-Access-Client-Secret = "CF_ACCESS_CLIENT_SECRET"'
	VARS="export CF_ACCESS_CLIENT_ID=...  CF_ACCESS_CLIENT_SECRET=..."
	;;
*)
	echo "MEM_MODE invalido: '$MEM_MODE'. Use 'token' ou 'cloudflare'." >&2; exit 1 ;;
esac

# ---- Claude Code: escopo project, versionado ------------------------------
cat >.mcp.json <<JSON
{
  "mcpServers": {
    "memoria": {
      "type": "http",
      "url": "${MEM_URL}",
      "headers": {
${CC_AUTH}
        "X-Project-Id": "${PROJECT_ID}"
      }
    }
  }
}
JSON

# ---- Codex ----------------------------------------------------------------
mkdir -p .codex
{
	echo '[mcp_servers.memoria]'
	echo "url = \"${MEM_URL}\""
	[[ -n "$CODEX_AUTH" ]] && echo "$CODEX_AUTH"
	echo
	echo '[mcp_servers.memoria.http_headers]'
	echo "X-Project-Id = \"${PROJECT_ID}\""
	[[ -n "$CODEX_ENV_HEADERS" ]] && echo "$CODEX_ENV_HEADERS"
} >.codex/config.toml

echo "projeto  : ${PROJECT_ID}"
echo "endpoint : ${MEM_URL}  (modo ${MEM_MODE})"
echo "gerados  : .mcp.json, .codex/config.toml"
echo
echo "Sem segredo nos arquivos — pode commitar. No shell:"
echo "  ${VARS}"
