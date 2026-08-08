"""Barreira de segredos e PII, antes de qualquer coisa sair para o LLM.

Por que isto e bloqueante e nao um aviso: existem duas portas de escrita, e so
uma tem gate a montante. O caminho `/ingest` vem do `.ia`, onde o passo 6 do
Capture Protocol ja removeu credenciais. O caminho MCP `remember` vem de uma
pessoa digitando no Cowork — nao ha gate nenhum antes daqui. Como o Graphiti
manda o texto para um LLM externo extrair entidades, "gravou" e "vazou" sao o
mesmo evento.

Politica: alta confianca bloqueia; nada e "sanitizado" silenciosamente. Corrigir
o texto por conta propria produziria uma memoria adulterada que ninguem revisou
— pior que recusar.

O detector nao echoa o valor casado em lugar nenhum: nem no erro, nem no log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    hint: str


# --------------------------------------------------------------------------- #
# Segredos — formatos com prefixo proprio, praticamente sem falso positivo
# --------------------------------------------------------------------------- #

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("chave privada", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("token do GitHub", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("token do GitHub (fine-grained)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("chave da OpenAI", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("chave da Anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("token do Slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("chave do Google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("token do Stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("token do Cloudflare Access", re.compile(r"\b[0-9a-f]{64}\.access\b")),
    ("senha em connection string", re.compile(r"\b[a-z+]{2,12}://[^\s:@/]+:[^\s@/]{3,}@")),
    (
        "credencial em atribuicao",
        re.compile(
            r"(?i)\b(?:password|passwd|senha|secret|api[_-]?key|access[_-]?token|"
            r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',;]{8,}"
        ),
    ),
]

# Placeholder obvio nao e segredo. Sem isto, documentar um exemplo vira erro.
_PLACEHOLDER = re.compile(
    r"(?i)(?:\$\{[^}]+\}|<[^>]{2,40}>|\bxxx+\b|\bchangeme\b|\bexample\b|"
    r"\bplaceholder\b|\bredacted\b|\bseu[-_]?token\b|\bdummy\b|\bfake\b|\btroque\b|"
    r"\*{4,}|\.{3,})"
)


# --------------------------------------------------------------------------- #
# PII brasileira com digito verificador — so casa se o numero for valido
# --------------------------------------------------------------------------- #

_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def _cpf_valido(raw: str) -> bool:
    cpf = _digits(raw)
    if len(cpf) != 11 or len(set(cpf)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(cpf[i]) * (size + 1 - i) for i in range(size))
        check = (total * 10) % 11 % 10
        if check != int(cpf[size]):
            return False
    return True


def _cnpj_valido(raw: str) -> bool:
    cnpj = _digits(raw)
    if len(cnpj) != 14 or len(set(cnpj)) == 1:
        return False
    for size, weights in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]),
                          (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        total = sum(int(cnpj[i]) * weights[i] for i in range(size))
        check = 0 if total % 11 < 2 else 11 - total % 11
        if check != int(cnpj[size]):
            return False
    return True


def _luhn_valido(raw: str) -> bool:
    num = _digits(raw)
    if not 13 <= len(num) <= 19 or len(set(num)) == 1:
        return False
    total, alt = 0, False
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def scan(text: str) -> list[Finding]:
    """Devolve o que foi encontrado. Lista vazia significa liberado."""
    if not text:
        return []

    findings: list[Finding] = []

    for kind, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if _PLACEHOLDER.search(match.group(0)):
                continue
            findings.append(Finding(kind, "segredo"))
            break

    for match in _CPF_RE.finditer(text):
        if _cpf_valido(match.group(0)):
            findings.append(Finding("CPF", "dado pessoal"))
            break

    for match in _CNPJ_RE.finditer(text):
        if _cnpj_valido(match.group(0)):
            findings.append(Finding("CNPJ", "dado pessoal"))
            break

    for match in _CARD_RE.finditer(text):
        raw = match.group(0)
        # CPF valido tambem passa no Luhn as vezes; ja foi reportado acima.
        if _cpf_valido(raw) or _cnpj_valido(raw):
            continue
        if _luhn_valido(raw):
            findings.append(Finding("numero de cartao", "dado financeiro"))
            break

    return findings


def assert_clean(text: str, *, context: str = "conteudo") -> None:
    """Levanta ValueError se houver achado. Nunca inclui o valor casado."""
    findings = scan(text)
    if not findings:
        return
    tipos = ", ".join(sorted({f.kind for f in findings}))
    raise ValueError(
        f"Recusado: o {context} parece conter {tipos}. "
        "Memoria e enviada a um LLM externo para extracao — gravar e vazar sao o "
        "mesmo evento aqui. Remova o dado e reescreva o texto; o servidor nao "
        "edita por voce, para nao gravar uma versao que ninguem revisou."
    )
