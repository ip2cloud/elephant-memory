"""Middleware ASGI: identidade, escopo de projeto e rate limit.

Dois modos de identidade, via AUTH_MODE:

  cloudflare  Valida o JWT que o Cloudflare Access injeta em
              `Cf-Access-Jwt-Assertion`. O user_id sai de um claim VERIFICADO.
              Use em producao.

  token       Bearer estatico conferido contra AUTH_TOKENS. Para rodar local
              sem Cloudflare no caminho.

Por que validar o JWT mesmo com o Cloudflare na frente: "estar atras da
Cloudflare" so e fronteira de seguranca se o origin nao puder ser alcancado
direto. Com Tunnel, nao pode — e ainda assim validamos, porque uma mudanca de
topologia no futuro nao deve reabrir o servidor silenciosamente.

O projeto e resolvido, nesta ordem:
  1. prefixo de caminho  /p/<projeto>/mcp   (clientes que so aceitam URL)
  2. header              X-Project-Id
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope as ASGIScope, Send

from .config import env
from .registry import assert_grants_within_registry, load_registry
from .scope import ScopeError, normalize_project_id, parse_grants, reset_scope, set_scope

logger = logging.getLogger("graphmem.auth")

PUBLIC_PATHS = {"/healthz"}
PATH_SCOPE_RE = re.compile(r"^/p/([^/]+)(/.*)?$")


# --------------------------------------------------------------------------- #
# Identidade
# --------------------------------------------------------------------------- #


class StaticTokenVerifier:
    """Bearer estatico. AUTH_TOKENS aceita duas formas:

        {"tok_...": {"user": "alfredo",
                     "scopes": ["read:scout-manager", "write:scout-manager"]}}

    Permissao e sempre POR PROJETO. A forma curta ({"tok_...": "alfredo"})
    ainda e aceita e concede ZERO grants — serve para declarar a identidade
    sem dar acesso a projeto nenhum.
    """

    def __init__(self, tokens: dict[str, tuple[str, frozenset[str]]]):
        self.tokens = tokens

    @classmethod
    def from_env(cls) -> "StaticTokenVerifier":
        raw = (env("AUTH_TOKENS") or "").strip()
        if not raw:
            raise RuntimeError(
                "AUTH_TOKENS nao definido. Gere um token com:\n"
                "  python3 -c \"import secrets;print('tok_'+secrets.token_urlsafe(32))\""
            )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeError("AUTH_TOKENS deve ser um objeto JSON nao-vazio.")

        tokens: dict[str, tuple[str, frozenset[str]]] = {}
        for token, value in parsed.items():
            if len(token) < 24:
                raise RuntimeError(f"Token muito curto para {value!r} (minimo 24 chars).")
            if isinstance(value, str):
                tokens[token] = (value, frozenset())
            elif isinstance(value, dict):
                user = value.get("user")
                if not user:
                    raise RuntimeError(f"Entrada de AUTH_TOKENS sem 'user': {value!r}")
                tokens[token] = (str(user), parse_grants(value.get("scopes")))
            else:
                raise RuntimeError(f"Entrada de AUTH_TOKENS invalida: {value!r}")
        return cls(tokens)

    def identify(self, headers: dict[str, str]) -> tuple[str, frozenset[str]] | None:
        prefix, _, presented = headers.get("authorization", "").partition(" ")
        if prefix.lower() not in {"bearer", "token"} or not presented:
            return None
        presented = presented.strip()
        # Percorre o mapa inteiro, sem short-circuit: nao vaza por timing
        # quantos tokens existem nem qual bateu.
        found: tuple[str, frozenset[str]] | None = None
        for token, value in self.tokens.items():
            if hmac.compare_digest(token, presented):
                found = value
        return found


class CloudflareAccessVerifier:
    """Valida `Cf-Access-Jwt-Assertion` contra o JWKS do seu time.

    O Cloudflare rotaciona as chaves de assinatura a cada ~6 semanas (as
    antigas seguem validas por 7 dias). Por isso o JWKS e buscado com cache
    curto pelo PyJWKClient, e nunca embutido na imagem.
    """

    def __init__(
        self,
        team_domain: str,
        audience: str,
        claim: str = "common_name",
        grants: dict[str, frozenset[str]] | None = None,
    ):
        self.audience = audience
        self.claim = claim
        # identidade -> escopos. Ausente = so leitura.
        self.grants = grants or {}
        self.issuer = f"https://{team_domain}"
        self.jwks = PyJWKClient(
            f"https://{team_domain}/cdn-cgi/access/certs",
            cache_keys=True,
            lifespan=3600,
        )

    @classmethod
    def from_env(cls) -> "CloudflareAccessVerifier":
        team = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip()
        aud = os.environ.get("CF_ACCESS_AUD", "").strip()
        if not team or not aud:
            raise RuntimeError(
                "AUTH_MODE=cloudflare exige CF_ACCESS_TEAM_DOMAIN "
                "(ex: seutime.cloudflareaccess.com) e CF_ACCESS_AUD "
                "(o AUD tag da aplicacao, em Access > Application > Overview)."
            )
        raw_grants = (env("CF_ACCESS_GRANTS") or "").strip()
        grants: dict[str, frozenset[str]] = {}
        if raw_grants:
            try:
                for identity, scopes in json.loads(raw_grants).items():
                    grants[str(identity)] = parse_grants(scopes)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"CF_ACCESS_GRANTS nao e JSON valido: {exc}") from exc
        return cls(
            team,
            aud,
            os.environ.get("CF_ACCESS_IDENTITY_CLAIM", "common_name"),
            grants,
        )

    def identify(self, headers: dict[str, str]) -> tuple[str, frozenset[str]] | None:
        token = headers.get("cf-access-jwt-assertion", "")
        if not token:
            return None
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("JWT do Access rejeitado: %s", exc)
            return None
        # Service token -> `common_name`. Login humano -> `email`.
        identity = claims.get(self.claim) or claims.get("email") or claims.get("sub")
        if not identity:
            return None
        identity = str(identity)
        return identity, self.grants.get(identity, frozenset())


def build_verifier():
    mode = os.environ.get("AUTH_MODE", "cloudflare").strip().lower()
    if mode == "cloudflare":
        verifier = CloudflareAccessVerifier.from_env()
        todos = list(verifier.grants.values())
    elif mode == "token":
        verifier = StaticTokenVerifier.from_env()
        todos = [g for _, g in verifier.tokens.values()]
    else:
        raise RuntimeError(f"AUTH_MODE invalido: {mode!r}. Use 'cloudflare' ou 'token'.")

    # Boot falha se uma credencial referenciar projeto fora do registro.
    assert_grants_within_registry(load_registry(), todos)
    return verifier


# --------------------------------------------------------------------------- #
# Rate limit
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Janela deslizante por identidade. Em memoria, por processo — segura loop
    de agente. Com mais de uma replica, vira por-replica: mova pra borda."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


class ScopeMiddleware:
    def __init__(self, app: ASGIApp, verifier=None):
        self.app = app
        self.verifier = verifier or build_verifier()
        self.limiter = RateLimiter(
            limit=int(os.environ.get("RATE_LIMIT", "120")),
            window_seconds=int(os.environ.get("RATE_WINDOW", "60")),
        )
        logger.info("auth pronta: %s", type(self.verifier).__name__)

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Prefixo /p/<projeto>/... : tira do caminho antes de repassar, senao o
        # transporte MCP nao reconhece a rota.
        path_project: str | None = None
        match = PATH_SCOPE_RE.match(path)
        if match:
            path_project = match.group(1)
            rest = match.group(2) or "/"
            scope = dict(scope)
            scope["path"] = rest
            scope["raw_path"] = rest.encode()
            path = rest

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        identified = self.verifier.identify(headers)
        if identified is None:
            return await self._deny(scope, receive, send, 401, "Nao autenticado.")
        user_id, grants = identified

        if not self.limiter.allow(user_id):
            return await self._deny(scope, receive, send, 429, "Rate limit excedido.")

        try:
            project_id = normalize_project_id(path_project or headers.get("x-project-id", ""))
        except ScopeError as exc:
            return await self._deny(scope, receive, send, 400, str(exc))

        token = set_scope(user_id=user_id, project_id=project_id, grants=grants)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_scope(token)

    @staticmethod
    async def _deny(scope, receive, send, status: int, detail: str) -> None:
        await JSONResponse({"error": detail}, status_code=status)(scope, receive, send)
