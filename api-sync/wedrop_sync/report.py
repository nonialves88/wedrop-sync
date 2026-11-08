"""Resumo legível e política de alerta.

Regra do produto: **só avisar quando houver problema real** — falha de login,
pedido de código por e-mail, integração que perdeu autorização, pico de erro.
Ruído conhecido do catálogo (anúncio que não existe mais, `under_review`,
`inactive`, `closed`, item isolado) nunca gera mensagem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .classify import BENIGN_REASONS
from .runner import ChannelResult, RunResult

REASON_LABELS = {
    "ok": "ok",
    "partial": "atualização parcial",
    "skipped": "pulado",
    "orphan": "anúncio não existe mais no marketplace (ignorado)",
    "channel_status": "anúncio em revisão/inativo/fechado (ignorado)",
    "auth_loss": "perdeu a autorização da loja",
    "missing_model_id": "anúncio com variação sem model_id (precisa reimportar)",
    "rate_limit": "limite de requisições da API",
    "http_5xx": "erro do servidor do marketplace (5xx)",
    "network": "falha de rede",
    "timeout": "tempo esgotado",
    "item_error": "erro no anúncio",
    "unknown": "resposta não reconhecida",
}


def label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason)


@dataclass
class Alert:
    kind: str
    severity: str  # fatal | high | medium
    title: str
    detail: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "severity": self.severity, "title": self.title, "detail": self.detail}


# --------------------------------------------------------------------------- #
# Formatação
# --------------------------------------------------------------------------- #
def format_summary_line(channel: ChannelResult) -> str:
    """Linha estável, fácil de ler com grep/tail (mesmo formato do log)."""
    return (
        f"SUMMARY {channel.key} mapeados={channel.mapped_total} selecionados={channel.selected} "
        f"ok={channel.ok} parcial={channel.partial} pulado={channel.skipped} "
        f"erro={channel.errors} timeout={channel.timeouts} duracao={channel.duration:.0f}s"
    )


def format_reasons(channel: ChannelResult, limit: int = 4) -> str:
    parts = []
    for reason, count in channel.reasons.most_common():
        if reason in ("ok", "skipped", "partial") and count:
            continue
        parts.append(f"{label(reason)}={count}")
    if not parts:
        return ""
    return ", ".join(parts[:limit])


def format_channel_line(channel: ChannelResult) -> str:
    status = {
        "ok": "ok",
        "dry-run": "dry-run",
        "aborted": "ABORTADO",
        "empty": "sem itens",
    }.get(channel.status, channel.status)
    extra = format_reasons(channel)
    detail = f" · {extra}" if extra else ""
    return (
        f"  {channel.key:<7} {status:<9} mapeados={channel.mapped_total} selecionados={channel.selected} "
        f"ok={channel.ok} erro={channel.errors} timeout={channel.timeouts} ({_short_duration(channel.duration)}){detail}"
    )


def _short_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}min"
    if minutes:
        return f"{minutes}min{secs:02d}s"
    return f"{secs}s"


def format_run_summary(run: RunResult) -> str:
    lines = [
        f"Rodada {run.started_at} · modo={run.mode} · duração={_short_duration(run.duration_s)}"
        + (f" · logins={run.logins}" if run.logins else ""),
    ]
    for channel in run.channels:
        lines.append(format_channel_line(channel))
    if run.fatal:
        lines.append(f"  FALHA: {run.fatal}")
    if run.interrupted:
        lines.append("  (rodada interrompida)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Política de alerta
# --------------------------------------------------------------------------- #
def _serious(channel: ChannelResult) -> int:
    """Erros que importam: exclui o ruído de catálogo (órfão, revisão, fechado)."""
    serious = channel.errors + channel.timeouts
    for reason in BENIGN_REASONS:
        serious -= 0  # órfãos são contados como SKIPPED, não como erro
    return max(0, serious)


def _spike(channel: ChannelResult, settings) -> bool:
    serious = _serious(channel)
    if serious <= 0:
        return False
    processed = max(1, channel.processed)
    if serious < settings.notify_min_errors:
        return False
    return (serious / processed) >= settings.notify_error_rate


def decide_alerts(run: RunResult, settings, previous: Optional[Dict[str, Any]] = None) -> List[Alert]:
    """Converte a rodada em uma lista curta de motivos para avisar o responsável."""
    alerts: List[Alert] = []

    if run.fatal_kind:
        if run.fatal_kind == "email_code":
            alerts.append(
                Alert(
                    "email_code",
                    "fatal",
                    "o WeDrop pediu código de verificação por e-mail",
                    "é preciso entrar manualmente uma vez para reativar a sessão",
                )
            )
        elif run.fatal_kind in ("invalid_credentials", "bad_response"):
            alerts.append(
                Alert(
                    "login_failed",
                    "fatal",
                    "não consegui entrar no WeDrop",
                    run.fatal or "verifique e-mail/senha em creds.env",
                )
            )
        else:
            alerts.append(
                Alert("login_failed", "high", "falha ao falar com a API do WeDrop", run.fatal)
            )

    for channel in run.channels:
        auth_loss = channel.reasons.get("auth_loss", 0)
        if auth_loss:
            alerts.append(
                Alert(
                    "authorization_lost",
                    "high",
                    f"{channel.name}: {auth_loss} anúncio(s) com perda de autorização",
                    "reconecte esta integração em Minhas integrações"
                    + (f" — canal abortado: {channel.aborted_reason}" if channel.aborted_reason else ""),
                )
            )
            continue
        if channel.status == "aborted" and not channel.aborted_reason.startswith("interrompido"):
            alerts.append(
                Alert("channel_aborted", "high", f"{channel.name}: canal abortado", channel.aborted_reason)
            )
            continue
        if _spike(channel, settings):
            serious = _serious(channel)
            rate = serious / max(1, channel.processed) * 100
            alerts.append(
                Alert(
                    "error_spike",
                    "high",
                    f"{channel.name}: {serious} erro(s) em {channel.processed} ({rate:.0f}%)",
                    format_reasons(channel) or "veja last-run.log",
                )
            )

    if run.interrupted:
        alerts.append(Alert("interrupted", "medium", "rodada interrompida antes de terminar", ""))

    if previous and alerts:
        prev_errors = sum(
            int(ch.get("counts", {}).get("error", 0)) for ch in previous.get("channels", []) or []
        )
        if prev_errors and run.total_errors > prev_errors * 2.5:
            alerts.append(
                Alert(
                    "error_trend",
                    "medium",
                    "aumento de erros em relação à rodada anterior",
                    f"{prev_errors} → {run.total_errors}",
                )
            )

    return alerts


def needs_human(alerts: Iterable[Alert]) -> bool:
    return any(alert.severity in ("fatal", "high") for alert in alerts)


def format_alert_message(run: RunResult, alerts: List[Alert]) -> str:
    lines = ["⚠️ WeDrop — sincronização de estoque precisa de atenção", ""]
    for alert in alerts:
        prefix = "[FALHA]" if alert.severity == "fatal" else "[ATENÇÃO]"
        lines.append(f"{prefix} {alert.title}")
        if alert.detail:
            lines.append(f"    {alert.detail}")
    lines.append("")
    lines.append("Resumo da rodada:")
    for channel in run.channels:
        lines.append(
            f"- {channel.name}: selecionados={channel.selected} ok={channel.ok} "
            f"erro={channel.errors} timeout={channel.timeouts}"
            + (f" ({channel.aborted_reason})" if channel.aborted_reason else "")
        )
    if not run.channels:
        lines.append("- nenhum canal chegou a rodar")
    reasons = format_reasons_for_run(run)
    if reasons:
        lines.append("")
        lines.append(f"Motivos mais comuns: {reasons}")
    lines.append("")
    lines.append(f"Duração: {_short_duration(run.duration_s)} · registro completo em last-run.log")
    return "\n".join(lines)


def format_reasons_for_run(run: RunResult, limit: int = 5) -> str:
    merged: Dict[str, int] = {}
    for channel in run.channels:
        for reason, count in channel.reasons.items():
            if reason in ("ok", "skipped", "partial"):
                continue
            merged[reason] = merged.get(reason, 0) + count
    if not merged:
        return ""
    ordered = sorted(merged.items(), key=lambda item: item[1], reverse=True)
    return ", ".join(f"{label(reason)}={count}" for reason, count in ordered[:limit])


# --------------------------------------------------------------------------- #
# Histórico (`status`)
# --------------------------------------------------------------------------- #
def format_history(records: List[Dict[str, Any]], limit: int = 10) -> str:
    if not records:
        return "nenhuma rodada registrada ainda"
    lines = [f"{'quando':<20} {'modo':<8} {'duração':>9}  canais"]
    for record in records[-limit:][::-1]:
        when = str(record.get("started_at", ""))[:19].replace("T", " ")
        mode = str(record.get("mode", "?"))[:8]
        duration = _short_duration(float(record.get("duration_s", 0) or 0))
        channels = record.get("channels") or []
        parts = []
        for channel in channels:
            counts = channel.get("counts", {}) or {}
            parts.append(
                f"{channel.get('key')}:ok={counts.get('ok', 0)}/erro={counts.get('error', 0)}"
            )
        fatal = " FALHA" if record.get("fatal") else ""
        lines.append(f"{when:<20} {mode:<8} {duration:>9}  {' '.join(parts) or '-'}{fatal}")
    return "\n".join(lines)


def compact_record(run: RunResult, alerts: List[Alert]) -> Dict[str, Any]:
    return {
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_s": round(run.duration_s, 1),
        "mode": run.mode,
        "fatal": run.fatal,
        "fatal_kind": run.fatal_kind,
        "interrupted": run.interrupted,
        "totals": {
            "processed": run.total_processed,
            "ok": run.total_ok,
            "errors": run.total_errors,
            "timeouts": run.total_timeouts,
        },
        "channels": [
            {
                "key": channel.key,
                "name": channel.name,
                "status": channel.status,
                "mapped_total": channel.mapped_total,
                "selected": channel.selected,
                "counts": dict(channel.counts),
                "reasons": dict(channel.reasons),
                "duration_s": round(channel.duration, 1),
                "aborted_reason": channel.aborted_reason,
            }
            for channel in run.channels
        ],
        "alerts": [alert.to_dict() for alert in alerts],
    }
