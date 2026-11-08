"""Classificação das respostas da API.

A API responde HTTP 200 mesmo quando deu problema: o corpo traz `success`,
`partialSuccess`, `skipped` e `errors`. Aqui replicamos a leitura do front-end
(função `YN()`) e reduzimos tudo a um `Verdict` com um **bucket de motivo** —
que é o que permite gerar resumo, decidir alerta e detectar pico de erro sem
ficar lendo log no olho.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional


class Outcome(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    ERROR = "error"
    TIMEOUT = "timeout"

    def __str__(self) -> str:  # aparece no SUMMARY
        return self.value


# Motivos (buckets) — chaves estáveis, usadas em relatórios e na política de alerta.
R_OK = "ok"
R_PARTIAL = "partial"
R_SKIPPED = "skipped"
R_ORPHAN = "orphan"                 # anúncio não existe mais no marketplace
R_CHANNEL_STATUS = "channel_status"  # under_review / inactive / closed
R_AUTH_LOSS = "auth_loss"           # perdeu autorização da loja
R_MISSING_MODEL = "missing_model_id"  # variações da Shopee sem model_id
R_RATE_LIMIT = "rate_limit"
R_HTTP_5XX = "http_5xx"
R_NETWORK = "network"
R_TIMEOUT = "timeout"
R_ITEM_ERROR = "item_error"         # erro de item não classificado
R_UNKNOWN = "unknown"

#: Falhas que **não** merecem alerta: são ruído conhecido do catálogo.
BENIGN_REASONS = frozenset({R_ORPHAN, R_CHANNEL_STATUS, R_SKIPPED})

_SKIP_MARKERS = (
    "orphan",
    "nao existe",
    "not found",
    "nao encontrado",
    "inexistente",
    "deleted",
    "removido",
    "nao encontrou este anuncio",
    "anuncio nao encontrado",
)

_STATUS_MARKERS = (
    "under_review",
    "under review",
    "inactive",
    "inativo",
    "closed",
    "fechado",
    "em revisao",
    "pausado",
    "moderacao",
)

_AUTH_MARKERS = (
    "perdeu a autorizacao",
    "perdeu autorizacao",
    "lost authorization",
    "unauthorized",
    "not authorized",
    "forbidden",
    "nao autorizado",
    "invalid token",
    "token expirado",
    "token expired",
    "credenciais invalidas",
    "reconecte",
)

_MODEL_MARKERS = ("model_id", "model id")

_RATE_MARKERS = ("too many requests", "rate limit", "limite de requisicoes")


def normalize(text: Any) -> str:
    """Minúsculas e sem acento, para comparar mensagens em pt-BR sem sofrer com acentuação."""
    if text is None:
        return ""
    raw = unicodedata.normalize("NFKD", str(text))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return " ".join(raw.lower().split())


def _contains(text: str, markers: Iterable[str]) -> Optional[str]:
    for marker in markers:
        if marker in text:
            return marker
    return None


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reason: str
    message: str = ""
    retryable: bool = False
    #: informações úteis devolvidas pela API (ex.: {"quantidade_enviada": 4})
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def benign(self) -> bool:
        """Erro esperado de catálogo: registra e segue, nunca alerta."""
        return self.reason in BENIGN_REASONS

    @property
    def sent_quantity(self) -> Optional[int]:
        value = self.detail.get("quantidade_enviada") if self.detail else None
        return int(value) if isinstance(value, (int, float)) else None

    def describe(self) -> str:
        msg = f"{self.outcome}:{self.reason}"
        if self.message:
            msg = f"{msg} — {self.message}"
        if self.sent_quantity is not None:
            msg = f"{msg} (enviado: {self.sent_quantity})"
        return msg


def classify_message(message: Any) -> Verdict:
    """Traduz uma mensagem de erro da API em (outcome, motivo)."""
    text = normalize(message)
    if not text:
        return Verdict(Outcome.ERROR, R_UNKNOWN, "")

    if _contains(text, _AUTH_MARKERS):
        return Verdict(Outcome.ERROR, R_AUTH_LOSS, str(message))
    if _contains(text, _MODEL_MARKERS):
        return Verdict(Outcome.ERROR, R_MISSING_MODEL, str(message))
    if _contains(text, _SKIP_MARKERS):
        return Verdict(Outcome.SKIPPED, R_ORPHAN, str(message))
    if _contains(text, _STATUS_MARKERS):
        return Verdict(Outcome.SKIPPED, R_CHANNEL_STATUS, str(message))
    if _contains(text, _RATE_MARKERS):
        return Verdict(Outcome.ERROR, R_RATE_LIMIT, str(message), retryable=True)
    return Verdict(Outcome.ERROR, R_ITEM_ERROR, str(message))


def _first_error_message(body: dict) -> str:
    # O envelope real do update-stock é {"results": [{...sucesso por produto...}]}.
    results = body.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict) and not _truthy(entry.get("success")):
                message = _entry_message(entry)
                if message:
                    return message
    # `mensagem` vem em português e é a que interessa ao usuário — vem antes
    # das chaves técnicas (`error` traz "PUT ... falhou: ... HTTP 403").
    for key in ("mensagem", "errors", "message", "error", "detail", "reason"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value
    errors = body.get("errors")
    if isinstance(errors, dict):
        return str(errors.get("mensagem") or errors.get("message") or errors.get("error") or errors)
    if isinstance(errors, (list, tuple)):
        for entry in errors:
            if isinstance(entry, str) and entry.strip():
                return entry
            if isinstance(entry, dict):
                message = _entry_message(entry)
                if message:
                    return message
    return ""


def _entry_message(entry: dict) -> str:
    """Mensagem de erro de uma entrada de `errors`/`results` (formato real da API)."""
    for key in ("mensagem", "message", "error", "detail", "msg", "description", "reason", "stockSyncReason"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            inner = value.get("message") or value.get("error") or value.get("reason")
            if isinstance(inner, str) and inner.strip():
                return inner
    raw = entry.get("mlRaw") or entry.get("raw")
    if isinstance(raw, dict):
        for section in ("primary", "error", "response"):
            nested = raw.get(section)
            if isinstance(nested, dict):
                for key in ("message", "error", "reason"):
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
    return ""


def _from_results(results: list) -> Optional[Verdict]:
    """Lê o envelope observado ao vivo: {"results": [{success, availableQuantity, ...}]}."""
    entries = [entry for entry in results if isinstance(entry, dict)]
    if not entries:
        return None
    failures = []
    sent = 0
    for entry in entries:
        if not _truthy(entry.get("success")):
            failures.append(entry)
        qty = entry.get("availableQuantity")
        if isinstance(qty, (int, float)):
            sent += int(qty)
    detail = {"produtos": len(entries), "quantidade_enviada": sent}
    if not failures:
        return Verdict(Outcome.OK, R_OK, "", detail=detail)
    message = _entry_message(failures[0]) or "o canal recusou a atualização"
    base = classify_message(message)
    if len(failures) < len(entries) and base.outcome is Outcome.ERROR:
        return Verdict(Outcome.PARTIAL, base.reason, message, detail=detail)
    return Verdict(base.outcome, base.reason, message, detail=detail)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "ok", "sim")
    return False


def classify_update(status: int, body: Any) -> Verdict:
    """Classifica o retorno de POST /my-items/update-stock."""
    if status == 429:
        return Verdict(Outcome.ERROR, R_RATE_LIMIT, "limite de requisições", retryable=True)
    if status in (401, 403):
        return Verdict(Outcome.ERROR, R_AUTH_LOSS, f"HTTP {status}", retryable=False)
    if status >= 500:
        return Verdict(Outcome.ERROR, R_HTTP_5XX, f"HTTP {status}", retryable=False)
    if status >= 400:
        return Verdict(Outcome.ERROR, R_ITEM_ERROR, f"HTTP {status}")

    if not isinstance(body, dict):
        return Verdict(Outcome.OK, R_OK)  # 200 sem corpo: tratamos como enviado

    # Envelope observado ao vivo: {"results": [{success, availableQuantity, mlRaw...}]}
    results = body.get("results")
    if isinstance(results, list):
        from_results = _from_results(results)
        if from_results is not None:
            return from_results

    message = _first_error_message(body)
    if message:
        return classify_message(message)

    if _truthy(body.get("partialSuccess")) or _truthy(body.get("partial_success")):
        return Verdict(Outcome.PARTIAL, R_PARTIAL, "atualização parcial")
    if _truthy(body.get("skipped")):
        return Verdict(Outcome.SKIPPED, R_SKIPPED, "pulado pelo servidor")
    if "success" in body:
        return (
            Verdict(Outcome.OK, R_OK)
            if _truthy(body.get("success"))
            else Verdict(Outcome.ERROR, R_UNKNOWN, "success=false sem detalhe")
        )
    # Corpo 200 sem nenhuma das chaves conhecidas: aceitamos como enviado.
    return Verdict(Outcome.OK, R_OK)


def classify_read(status: int, body: Any) -> Verdict:
    """Classifica leituras (GET). Só interessa se deu para ler ou não."""
    if status == 429:
        return Verdict(Outcome.ERROR, R_RATE_LIMIT, "limite de requisições", retryable=True)
    if status in (401, 403):
        return Verdict(Outcome.ERROR, R_AUTH_LOSS, f"HTTP {status}")
    if status >= 500:
        return Verdict(Outcome.ERROR, R_HTTP_5XX, f"HTTP {status}", retryable=True)
    if status >= 400:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")
        return Verdict(Outcome.ERROR, R_ITEM_ERROR, f"HTTP {status} {detail}".strip())
    return Verdict(Outcome.OK, R_OK)


def auth_loss_rate(verdicts: Iterable[Verdict]) -> float:
    """Fração de erros que são perda de autorização — usado no circuit breaker."""
    total = errors = 0
    for verdict in verdicts:
        total += 1
        if verdict.reason == R_AUTH_LOSS:
            errors += 1
    return (errors / total) if total else 0.0
