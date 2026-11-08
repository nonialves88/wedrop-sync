"""Orquestração da rodada: um canal por vez, na ordem, com pool adaptativo.

Decisões herdadas (e mantidas) da versão anterior:
  * canais em sequência: Shopee → ML1 → ML2, cada um esperando o anterior;
  * anúncio inexistente/órfão é registrado e a rodada segue;
  * um erro de anúncio nunca derruba a rodada;
  * 5xx na atualização não é repetido; leitura é repetida;
  * trava exclusiva contra rodada simultânea (feita no cli, com lock.py).

Decisões novas (o "melhor que dá"):
  * concorrência adaptativa (`throttle.Gate`): abre/fecha conforme 429 e 5xx;
  * circuit breaker de autorização: um canal que "perdeu a autorização" em
    série é abortado em vez de queimar horas (o ML1 gastou 23 min para
    produzir 142 erros iguais);
  * `--resume`: retoma de onde parou usando o conjunto de itens já enviados;
  * verificação de resíduo: relê `outdatedStock=true` para provar que a
    atualização teve efeito (o "falso alarme" de 22/10);
  * artefato JSONL por canal, uma linha por anúncio.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .auth import AuthError, TokenProvider
from .classify import (
    Outcome,
    R_AUTH_LOSS,
    R_CHANNEL_STATUS,
    R_HTTP_5XX,
    R_RATE_LIMIT,
    Verdict,
    classify_update,
)
from .client import ApiClient, HttpTransport, RateLimited, TransportError
from .config import Channel, Credentials, Settings
from .items import Item, Queue, build_queue, extract_items
from .ratelimit import RateLimitProbe, SerializedPoster
from .state import ArtifactLog, done_ids_path, load_done_ids, save_done_ids, write_live
from .throttle import Gate

ISO = "%Y-%m-%dT%H:%M:%S%z"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime(ISO)


@dataclass
class ChannelResult:
    key: str
    name: str
    integration_id: int
    status: str = "ok"  # ok | dry-run | aborted | empty
    mapped_total: int = 0
    selected: int = 0
    outdated_total: Optional[int] = None
    counts: Dict[str, int] = field(default_factory=lambda: {o.value: 0 for o in Outcome})
    reasons: Counter = field(default_factory=Counter)
    sample_messages: Dict[str, str] = field(default_factory=dict)
    duration: float = 0.0
    aborted_reason: str = ""
    notes: List[str] = field(default_factory=list)
    residual_outdated: Optional[int] = None
    workers: int = 0
    #: `update-stock` saiu serializado (regra do marketplace, ver ratelimit.py)
    serialized: bool = True
    gate_stats: Dict[str, int] = field(default_factory=dict)
    #: soma do que o WeDrop efetivamente enviou aos canais (`availableQuantity`)
    sent_quantity: int = 0
    blocked_skipped: int = 0

    # ---- leituras úteis ----
    @property
    def ok(self) -> int:
        return self.counts.get(Outcome.OK.value, 0)

    @property
    def partial(self) -> int:
        return self.counts.get(Outcome.PARTIAL.value, 0)

    @property
    def skipped(self) -> int:
        return self.counts.get(Outcome.SKIPPED.value, 0)

    @property
    def errors(self) -> int:
        return self.counts.get(Outcome.ERROR.value, 0)

    @property
    def timeouts(self) -> int:
        return self.counts.get(Outcome.TIMEOUT.value, 0)

    @property
    def processed(self) -> int:
        return sum(self.counts.values())

    @property
    def success_rate(self) -> float:
        return (self.ok + self.partial) / self.processed if self.processed else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "integration_id": self.integration_id,
            "status": self.status,
            "mapped_total": self.mapped_total,
            "selected": self.selected,
            "outdated_total": self.outdated_total,
            "counts": dict(self.counts),
            "reasons": dict(self.reasons),
            "samples": dict(self.sample_messages),
            "duration_s": round(self.duration, 1),
            "aborted_reason": self.aborted_reason,
            "notes": list(self.notes),
            "residual_outdated": self.residual_outdated,
            "workers": self.workers,
            "serialized": self.serialized,
            "gate": dict(self.gate_stats),
            "sent_quantity": self.sent_quantity,
            "blocked_skipped": self.blocked_skipped,
        }


@dataclass
class RunResult:
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""
    duration_s: float = 0.0
    mode: str = "normal"  # normal | full | dry-run
    channels: List[ChannelResult] = field(default_factory=list)
    logins: int = 0
    fatal: str = ""
    fatal_kind: str = ""
    interrupted: bool = False
    host: str = ""
    version: str = "2.0.0"

    @property
    def total_ok(self) -> int:
        return sum(c.ok for c in self.channels)

    @property
    def total_errors(self) -> int:
        return sum(c.errors for c in self.channels)

    @property
    def total_timeouts(self) -> int:
        return sum(c.timeouts for c in self.channels)

    @property
    def total_processed(self) -> int:
        return sum(c.processed for c in self.channels)

    def channel(self, key: str) -> Optional[ChannelResult]:
        for channel in self.channels:
            if channel.key == key:
                return channel
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 1),
            "mode": self.mode,
            "logins": self.logins,
            "fatal": self.fatal,
            "fatal_kind": self.fatal_kind,
            "interrupted": self.interrupted,
            "host": self.host,
            "version": self.version,
            "totals": {
                "processed": self.total_processed,
                "ok": self.total_ok,
                "errors": self.total_errors,
                "timeouts": self.total_timeouts,
            },
            "channels": [channel.to_dict() for channel in self.channels],
        }


class _Progress:
    """Heartbeat no log — dá para acompanhar com `tail -f` sem navegador."""

    def __init__(self, logger: logging.Logger, key: str, every: float, total: int) -> None:
        self.log = logger
        self.key = key
        self.every = max(5.0, every)
        self.total = total
        self.started = time.monotonic()
        self._last = self.started
        self._lock = threading.Lock()

    def tick(self, result: ChannelResult) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last < self.every:
                return
            self._last = now
        elapsed = max(0.001, now - self.started)
        done = result.processed
        rate = done / elapsed
        eta = (self.total - done) / rate if rate > 0 else 0
        pct = (done / self.total * 100) if self.total else 0.0
        self.log.info(
            "progresso %s: %d/%d (%.0f%%) ok=%d erro=%d timeout=%d pulado=%d · %.1f/s · restam ~%s",
            self.key,
            done,
            self.total,
            pct,
            result.ok,
            result.errors,
            result.timeouts,
            result.skipped,
            rate,
            _human_eta(eta),
        )


def _human_eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:  # NaN / zero
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}min"
    if minutes:
        return f"{minutes}min{secs:02d}s"
    return f"{secs}s"


class Runner:
    def __init__(
        self,
        settings: Settings,
        credentials: Credentials,
        *,
        logger: Optional[logging.Logger] = None,
        transport: Optional[HttpTransport] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.log = logger or logging.getLogger("wedrop")
        self._clock = clock
        self._sleep = sleep
        self._on_event = on_event or (lambda event: None)
        self._stop = threading.Event()
        # estado ao vivo (barra de progresso do painel, vale para qualquer rodada)
        self._live_lock = threading.Lock()
        self._live_started = _now_iso()
        self._live_mode = ""
        self._live_results: Dict[str, ChannelResult] = {}
        self._live_last_write = 0.0
        self._live_running = True
        self._progress_emit: Dict[str, float] = {}
        self.transport = transport or HttpTransport(
            settings.api_base,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
            backoff=settings.retry_backoff,
            default_retry_after=settings.default_retry_after,
        )
        self.tokens = TokenProvider(self.transport, credentials)
        self.api = ApiClient(
            self.transport,
            self.tokens,
            read_attempts=settings.read_attempts,
            write_attempts=settings.update_attempts,
            on_event=self._on_client_event,
        )
        self.user_id: Optional[int] = None

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Interrupção cooperativa (SIGTERM/SIGINT): termina o que está em voo e sai."""
        if not self._stop.is_set():
            self.log.warning("interrupção pedida: encerrando com segurança…")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _on_client_event(self, event: str, **fields: Any) -> None:
        if event == "rate_limited":
            self.log.warning(
                "429 na API (%s): aguardando %ss antes de continuar", fields.get("path"), fields.get("seconds")
            )
            self._emit("rate_limited", seconds=fields.get("seconds"))
        elif event == "token_rejected":
            self.log.warning("token recusado (HTTP %s): renovando login", fields.get("status"))
            self._emit("token_rejected", status=fields.get("status"))

    def _emit(self, type_: str, **fields: Any) -> None:
        """Evento estruturado (usado pela UI e por quem quiser acompanhar de fora)."""
        try:
            self._on_event({"type": type_, "ts": time.time(), **fields})
        except Exception:  # um ouvinte com defeito nunca pode derrubar a rodada
            pass

    # ------------------------------------------------------------------ #
    def _touch_live(self, force: bool = False) -> None:
        """Grava `state/live.json` — é o que permite a barra de progresso
        acompanhar rodada disparada por CLI/cron, não só pelo painel."""
        agora = time.monotonic()
        with self._live_lock:
            if not force and (agora - self._live_last_write) < 2.0:
                return
            self._live_last_write = agora
            canais = {}
            for key, resultado in self._live_results.items():
                canais[key] = {
                    "name": resultado.name,
                    "status": resultado.status,
                    "selected": resultado.selected,
                    "done": resultado.processed,
                    "ok": resultado.ok,
                    "partial": resultado.partial,
                    "skipped": resultado.skipped,
                    "error": resultado.errors,
                    "timeout": resultado.timeouts,
                    "sent_quantity": resultado.sent_quantity,
                    "duration_s": round(resultado.duration, 1),
                    "aborted_reason": resultado.aborted_reason,
                }
            payload = {
                "running": self._live_running,
                "mode": self._live_mode,
                "started_at": self._live_started,
                "updated_at": _now_iso(),
                "parallel": bool(self.settings.parallel_channels),
                "channels": canais,
                "totals": {
                    "done": sum(c["done"] for c in canais.values()),
                    "selected": sum(c["selected"] for c in canais.values()),
                    "ok": sum(c["ok"] for c in canais.values()),
                    "skipped": sum(c["skipped"] for c in canais.values()),
                    "error": sum(c["error"] for c in canais.values()),
                },
            }
        write_live(self.settings.state_dir, payload)

    # ------------------------------------------------------------------ #
    def run(self) -> RunResult:
        settings = self.settings
        started = time.time()
        result = RunResult(
            started_at=_now_iso(),
            mode="dry-run" if settings.dry_run else ("full" if settings.all_items else "normal"),
            host=_hostname(),
        )
        self.log.info(
            "rodada iniciada · modo=%s · canais=%s · workers=%d · delay=%.2fs%s",
            result.mode,
            ",".join(settings.channel_keys()),
            settings.workers,
            settings.delay,
            " · retomando" if settings.resume else "",
        )
        self._emit(
            "run_start",
            mode=result.mode,
            channels=[channel.key for channel in settings.order],
            dry_run=settings.dry_run,
            workers=settings.workers,
            delay=settings.delay,
        )
        self._live_mode = result.mode
        self._live_running = True
        self._touch_live(force=True)
        try:
            self.user_id = self._fetch_user_id()
            if settings.parallel_channels and len(settings.order) > 1:
                self._run_channels_parallel(result)
            else:
                for channel in settings.order:
                    if self._stop.is_set():
                        self.log.warning("rodada interrompida antes de %s", channel.key)
                        result.interrupted = True
                        break
                    channel_result = self._run_channel(channel)
                    result.channels.append(channel_result)
        except AuthError as exc:
            result.fatal_kind = exc.kind
            result.fatal = str(exc)
            self.log.error("falha de autenticação [%s]: %s", exc.kind, exc)
            self._emit("auth_error", kind=exc.kind, message=str(exc))
        except KeyboardInterrupt:  # pragma: no cover - interativo
            result.interrupted = True
            self.log.warning("interrompido pelo teclado")
        finally:
            result.logins = self.tokens.logins
            result.finished_at = _now_iso()
            result.duration_s = time.time() - started
            try:
                self.transport.close()
            except Exception:
                pass
            self.log.info(
                "rodada encerrada em %s · processados=%d ok=%d erros=%d timeouts=%d",
                _human_eta(result.duration_s),
                result.total_processed,
                result.total_ok,
                result.total_errors,
                result.total_timeouts,
            )
            self._emit(
                "run_done",
                mode=result.mode,
                duration_s=round(result.duration_s, 1),
                processed=result.total_processed,
                ok=result.total_ok,
                errors=result.total_errors,
                timeouts=result.total_timeouts,
                fatal=result.fatal,
                channels=[channel.to_dict() for channel in result.channels],
            )
            self._live_running = False
            self._touch_live(force=True)
        return result

    # ------------------------------------------------------------------ #
    def _run_channels_parallel(self, result: RunResult) -> None:
        """Roda os canais ao mesmo tempo, como fazer o processo em 3 navegadores.

        Diferença de segurança: as duas contas do Mercado Livre compartilham o
        mesmo orçamento de rate limit, então elas se revezam (mesma faixa); a
        Shopee corre em paralelo porque é outro marketplace.
        """
        lanes: Dict[str, List[Channel]] = {}
        for channel in self.settings.order:
            lanes.setdefault(channel.lane, []).append(channel)

        posters = {
            lane: SerializedPoster(self.settings.delay, strict=self.settings.serialize_updates)
            for lane in lanes
        }
        self.log.info(
            "modo paralelo: %s",
            " · ".join(f"{lane}:[{','.join(c.key for c in canais)}]" for lane, canais in lanes.items()),
        )
        self._emit("run_parallel", lanes={lane: [c.key for c in canais] for lane, canais in lanes.items()})

        resultados: Dict[str, ChannelResult] = {}
        trava = threading.Lock()

        def trabalhar(lane: str) -> None:
            for channel in lanes[lane]:
                if self._stop.is_set():
                    return
                canal_result = self._run_channel(channel, poster=posters[lane])
                with trava:
                    resultados[channel.key] = canal_result

        with ThreadPoolExecutor(max_workers=len(lanes), thread_name_prefix="lane") as pool:
            list(pool.map(trabalhar, list(lanes)))

        for channel in self.settings.order:  # mantém a ordem original no relatório
            canal_result = resultados.get(channel.key)
            if canal_result is not None:
                result.channels.append(canal_result)
        if self._stop.is_set():
            result.interrupted = True

    # ------------------------------------------------------------------ #
    def _fetch_user_id(self) -> Optional[int]:
        try:
            response = self.api.get("/me", attempts=self.settings.read_attempts)
        except TransportError as exc:
            self.log.warning("não consegui ler /me (%s); seguindo sem userId", exc)
            return None
        if response.status != 200:
            self.log.warning("GET /me respondeu HTTP %s; seguindo sem userId", response.status)
            return None
        for value in _walk(response.data, ("userId", "user_id", "id")):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _run_channel(self, channel: Channel, poster: Optional[SerializedPoster] = None) -> ChannelResult:
        settings = self.settings
        result = ChannelResult(key=channel.key, name=channel.name, integration_id=channel.integration_id)
        with self._live_lock:
            self._live_results[channel.key] = result
        self._touch_live(force=True)
        started = time.time()
        self.log.info("--- canal %s (integrationId=%s) ---", channel.name, channel.integration_id)

        try:
            queue = build_queue(
                self.api,
                integration_id=channel.integration_id,
                user_id=self.user_id,
                all_items=settings.all_items,
                page_size=settings.page_size,
                max_items=settings.max_items,
                log=lambda fmt, *a: self.log.info(fmt, *a),
            )
        except AuthError:
            raise
        except TransportError as exc:
            result.status = "aborted"
            result.aborted_reason = f"não consegui montar a fila: {exc}"
            result.duration = time.time() - started
            self.log.error("%s: %s", channel.key, result.aborted_reason)
            return result

        result.mapped_total = queue.mapped_total or 0
        result.outdated_total = queue.outdated_total
        result.selected = len(queue)
        result.notes.extend(queue.notes)
        self.log.info(
            "seleção %s: %d anúncio(s) · mapeados=%s · desatualizados=%s · duplicados removidos=%d%s",
            channel.key,
            len(queue),
            result.mapped_total,
            queue.outdated_total,
            queue.duplicates,
            " · TRUNCADO por --max" if queue.truncated else "",
        )
        bloqueados = sum(1 for item in queue.items if item.blocked)
        self._emit(
            "channel_start",
            channel=channel.key,
            name=channel.name,
            integration_id=channel.integration_id,
            mapped_total=result.mapped_total,
            selected=result.selected,
            outdated_total=queue.outdated_total,
            blocked=bloqueados,
            dry_run=settings.dry_run,
        )
        self._touch_live(force=True)

        if settings.dry_run:
            result.status = "dry-run"
            result.duration = time.time() - started
            self.log.info("dry-run %s: nada será enviado (%d itens na fila)", channel.key, len(queue))
            self._emit("channel_done", channel=channel.key, status=result.status, summary=result.to_dict())
            return result

        if not queue.items:
            result.status = "empty"
            result.notes.append("nenhum anúncio na seleção")
            result.duration = time.time() - started
            self.log.info("%s: nada a atualizar", channel.key)
            self._emit("channel_done", channel=channel.key, status=result.status, summary=result.to_dict())
            return result

        self._run_updates(channel, queue, result, poster)

        if settings.verify_samples > 0 and result.status == "ok" and not self._stop.is_set():
            result.residual_outdated = self._verify_residual(channel)
            if result.residual_outdated is not None:
                self.log.info(
                    "verificação %s: %d de %d relidos ainda aparecem com estoque desatualizado",
                    channel.key,
                    result.residual_outdated,
                    settings.verify_samples,
                )

        result.duration = time.time() - started
        self.log.info(
            "SUMMARY %s mapeados=%d selecionados=%d ok=%d parcial=%d pulado=%d erro=%d timeout=%d duracao=%.0fs",
            channel.key,
            result.mapped_total,
            result.selected,
            result.ok,
            result.partial,
            result.skipped,
            result.errors,
            result.timeouts,
            result.duration,
        )
        self._emit("channel_done", channel=channel.key, status=result.status, summary=result.to_dict())
        return result

    # ------------------------------------------------------------------ #
    def _run_updates(
        self,
        channel: Channel,
        queue: Queue,
        result: ChannelResult,
        poster: Optional[SerializedPoster] = None,
    ) -> None:
        settings = self.settings
        # Regra do marketplace: update-stock é sempre um por vez. O `--workers`
        # só acelera a *preparação*; os POSTs saem serializados (strict).
        # No modo paralelo o poster é compartilhado entre canais da mesma faixa.
        poster = poster or SerializedPoster(settings.delay, strict=settings.serialize_updates)
        workers = 1 if settings.serialize_updates else max(1, min(settings.workers, len(queue.items)))
        result.workers = workers
        result.serialized = settings.serialize_updates
        probe = RateLimitProbe(
            self.api,
            channel.integration_id,
            logger=self.log,
            sleep=self._sleep,
        )
        self._log_rate_gate(channel, probe)
        gate = Gate(workers, minimum=1)
        progress = _Progress(self.log, channel.key, settings.progress_every, len(queue.items))

        done_path = done_ids_path(settings.state_dir, channel.key)
        already_done = load_done_ids(done_path) if settings.resume else set()
        if already_done:
            remaining = [item for item in queue.items if item.key not in already_done]
            self.log.info("resume %s: %d item(ns) já enviados em rodada anterior, %d na fila",
                          channel.key, len(queue.items) - len(remaining), len(remaining))
            queue.items = remaining
            if not queue.items:
                result.status = "ok"
                result.notes.append("nada novo para enviar (resume)")
                return

        artifact_path = settings.artifacts_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{channel.key}.jsonl"
        artifact = ArtifactLog(artifact_path)
        breaker = threading.Event()
        auth_loss_streak = 0
        completed_ids: set = set(already_done)
        streak_lock = threading.Lock()

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wd")
        try:
            futures = {
                pool.submit(self._update_item, channel, item, poster, gate, breaker, probe): item
                for item in queue.items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    verdict = future.result()
                except AuthError as exc:
                    verdict = Verdict(Outcome.ERROR, R_AUTH_LOSS, f"autenticação: {exc}")
                except Exception as exc:  # nunca deixa um anúncio derrubar a rodada
                    verdict = Verdict(Outcome.ERROR, "item_error", f"{type(exc).__name__}: {exc}")

                self._record(result, item, verdict, artifact, completed_ids)
                progress.tick(result)
                self._emit_progress(channel, result, progress)
                if verdict.outcome is not Outcome.OK:
                    self._emit(
                        "item_result",
                        channel=channel.key,
                        item_id=str(item.item_id),
                        title=item.title[:60],
                        outcome=verdict.outcome.value,
                        reason=verdict.reason,
                        message=verdict.message[:160],
                    )

                with streak_lock:
                    if verdict.reason == R_AUTH_LOSS:
                        auth_loss_streak += 1
                    elif verdict.outcome in (Outcome.OK, Outcome.PARTIAL):
                        auth_loss_streak = 0

                    if auth_loss_streak >= settings.breaker_threshold and not breaker.is_set():
                        breaker.set()
                        result.status = "aborted"
                        result.aborted_reason = (
                            f"{auth_loss_streak} erros seguidos de perda de autorização — reconecte a integração"
                        )
                        self.log.error("%s: %s", channel.key, result.aborted_reason)

                if breaker.is_set() or self._stop.is_set():
                    break
        finally:
            if breaker.is_set() or self._stop.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
            else:
                pool.shutdown(wait=True)
            result.gate_stats = gate.stats()
            artifact.close()
            if completed_ids:
                save_done_ids(done_path, completed_ids)
            if self._stop.is_set() and result.status == "ok":
                result.status = "aborted"
                result.aborted_reason = "interrompido por sinal"

    # ------------------------------------------------------------------ #
    def _emit_progress(self, channel: Channel, result: ChannelResult, progress: "_Progress") -> None:
        """Heartbeat estruturado (a UI usa isto; o log tem o dele)."""
        now = time.monotonic()
        if now - self._progress_emit.get(channel.key, 0.0) < 0.8:
            return
        self._progress_emit[channel.key] = now
        elapsed = max(0.001, now - progress.started)
        done = result.processed
        rate = done / elapsed
        remaining = max(0, progress.total - done)
        self._emit(
            "progress",
            channel=channel.key,
            done=done,
            total=progress.total,
            ok=result.ok,
            partial=result.partial,
            skipped=result.skipped,
            errors=result.errors,
            timeouts=result.timeouts,
            sent_quantity=result.sent_quantity,
            rate=round(rate, 2),
            eta_s=int(remaining / rate) if rate > 0 else None,
        )
        self._touch_live()

    def _update_item(
        self,
        channel: Channel,
        item: Item,
        poster: SerializedPoster,
        gate: Gate,
        breaker: threading.Event,
        probe: RateLimitProbe,
    ) -> Verdict:
        if breaker.is_set() or self._stop.is_set():
            return Verdict(Outcome.SKIPPED, "skipped", "não enviado (rodada encerrando)")
        # O próprio WeDrop já disse que este anúncio não sincroniza (combo,
        # revisão no canal…). Não gastar requisição nem contar como erro.
        if item.blocked:
            return Verdict(
                Outcome.SKIPPED,
                R_CHANNEL_STATUS,
                item.sync_reason or "bloqueado pelo WeDrop",
            )
        gate.acquire()
        try:
            attempts = 0
            while True:
                attempts += 1
                if breaker.is_set() or self._stop.is_set():
                    return Verdict(Outcome.SKIPPED, "skipped", "não enviado (rodada encerrando)")
                # 1) fila do marketplace: uma chamada por vez, com intervalo
                poster.wait_turn()
                # 2) o gate do próprio WeDrop tem a palavra final
                probe.wait_if_needed()
                try:
                    response = self.api.post(
                        "/my-items/update-stock",
                        {"itemId": item.item_id, "integrationId": channel.integration_id},
                        attempts=self.settings.update_attempts,
                    )
                except RateLimited as limited:
                    gate.on_rate_limited(limited.retry_after)
                    probe.note_429()
                    if attempts >= 3:
                        return Verdict(Outcome.ERROR, R_RATE_LIMIT, "limite de requisições esgotou as tentativas")
                    wait = min(limited.retry_after, 120.0)
                    self.log.warning("429 no item %s: aguardando %.0fs", item.item_id, wait)
                    self._sleep(wait)
                    continue
                except TransportError as exc:
                    return Verdict(Outcome.TIMEOUT, "timeout", str(exc))
                except AuthError as exc:
                    return Verdict(Outcome.ERROR, R_AUTH_LOSS, f"autenticação: {exc}")

                verdict = classify_update(response.status, response.data)
                if verdict.outcome in (Outcome.OK, Outcome.PARTIAL):
                    gate.on_success()
                elif verdict.reason == R_HTTP_5XX:
                    gate.on_server_error()
                elif verdict.reason == R_RATE_LIMIT:
                    probe.note_429()
                return verdict
        finally:
            gate.release()

    # ------------------------------------------------------------------ #
    def _log_rate_gate(self, channel: Channel, probe: RateLimitProbe) -> None:
        """Mostra o estado do gate do WeDrop no início do canal (transparência)."""
        estado = probe.refresh()
        if not estado:
            self.log.info("%s: gate de rate limit não exposto pela API; seguindo por ritmo + backoff de 429", channel.key)
            return
        self.log.info(
            "%s: gate do WeDrop · pode enviar=%s · requisições na janela=%s · 429 seguidos=%s · em backoff=%s",
            channel.key,
            estado.get("canMakeRequests"),
            estado.get("requestCount"),
            estado.get("consecutive429s"),
            estado.get("isInBackoff"),
        )
        self._emit(
            "rate_gate",
            channel=channel.key,
            can_make_requests=estado.get("canMakeRequests"),
            request_count=estado.get("requestCount"),
            consecutive_429=estado.get("consecutive429s"),
            in_backoff=estado.get("isInBackoff"),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _record(
        result: ChannelResult,
        item: Item,
        verdict: Verdict,
        artifact: ArtifactLog,
        completed_ids: set,
    ) -> None:
        result.counts[verdict.outcome.value] = result.counts.get(verdict.outcome.value, 0) + 1
        result.reasons[verdict.reason] += 1
        if verdict.reason not in result.sample_messages and verdict.message:
            result.sample_messages[verdict.reason] = verdict.message[:180]
        if item.blocked and verdict.outcome is Outcome.SKIPPED:
            result.blocked_skipped += 1
        sent = verdict.sent_quantity
        if sent is not None:
            result.sent_quantity += sent
        if verdict.outcome in (Outcome.OK, Outcome.PARTIAL):
            completed_ids.add(item.key)
        artifact.write(
            {
                "item_id": str(item.item_id),
                "title": item.title,
                "sku": item.sku,
                "outdated": item.outdated,
                "quantidade_publicada_antes": item.available_qty,
                "outcome": verdict.outcome.value,
                "reason": verdict.reason,
                "message": verdict.message[:300],
            }
        )

    def _verify_residual(self, channel: Channel) -> Optional[int]:
        try:
            response = self.api.get(
                "/my-items",
                params={
                    "integrationId": channel.integration_id,
                    "userId": self.user_id,
                    "situation": "mapped",
                    "limit": self.settings.verify_samples,
                    "outdatedStock": "true",
                },
                attempts=2,
            )
        except (TransportError, RateLimited, AuthError) as exc:
            self.log.warning("verificação de resíduo em %s falhou: %s", channel.key, exc)
            return None
        if response.status != 200:
            self.log.warning("verificação de resíduo em %s: HTTP %s", channel.key, response.status)
            return None
        return len(extract_items(response.data))


def _walk(data: Any, keys: tuple):
    """Procura o primeiro valor de `keys` em dicionários aninhados (até 2 níveis)."""
    if not isinstance(data, dict):
        return
    for key in keys:
        if key in data and data[key] not in (None, ""):
            yield data[key]
    for nested_key in ("data", "user", "profile", "account", "result"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested and nested[key] not in (None, ""):
                    yield nested[key]


def _hostname() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return "?"
