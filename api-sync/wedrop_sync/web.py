"""Painel local com um botão por integração.

    python -m wedrop_sync serve
    → http://127.0.0.1:8790

Três botões (Shopee / Mercado Livre 1 / Mercado Livre 2). Escolhe-se o critério
("estoque baixo ≤ N" ou "tudo"), opcionalmente simular, e clica. A rodada roda
em segundo plano no próprio processo, com progresso ao vivo (SSE).

Por que local e não um site publicado: a sessão é obtida por login na API do
WeDrop, então o processo fala direto com a API — nenhum navegador, nenhum
clique na tela. A senha fica no `creds.env` da máquina, nunca no navegador.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .auth import TokenProvider
from .client import ApiClient, HttpTransport
from .config import CHANNELS, Credentials, Settings, mask_email
from .items import extract_total
from .lock import RunLock, read_owner
from .runner import Runner
from .state import read_live

# O update-stock é sempre serializado (limite do marketplace). O que muda entre
# os ritmos é só o intervalo entre um envio e o próximo; o gate do WeDrop e o
# backoff de 429 continuam tendo a palavra final.
SPEEDS = {
    "cuidadoso": {"workers": 1, "delay": 5.0, "label": "cuidadoso · 5s entre envios"},
    "normal": {"workers": 1, "delay": 2.0, "label": "padrão · 2s entre envios"},
    "rapido": {"workers": 1, "delay": 0.5, "label": "agressivo · 0,5s (o gate decide)"},
}
SPEED_ALIASES = {"seguro": "cuidadoso", "lento": "cuidadoso", "fast": "rapido"}


def speed_preset(name: str) -> dict:
    key = SPEED_ALIASES.get(name, name)
    return SPEEDS.get(key, SPEEDS["normal"])


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class App:
    """Estado do painel: uma rodada por vez, progresso e assinantes do SSE."""

    def __init__(self, settings: Settings, credentials: Credentials, logger: Optional[logging.Logger] = None) -> None:
        self.base_settings = settings
        self.credentials = credentials
        self.log = logger or logging.getLogger("wedrop")
        self._lock = threading.Lock()
        self._subscribers: List[queue.Queue] = []
        self.job: Dict[str, Any] = {
            "status": "idle",
            "channel": None,
            "mode": None,
            "dry_run": True,
            "started_at": None,
            "done": 0,
            "total": 0,
            "counters": {"ok": 0, "partial": 0, "skipped": 0, "error": 0, "timeout": 0},
            "sent_quantity": 0,
            "eta_s": None,
            "rate": 0,
            "message": "",
            "summary": None,
        }
        self.recent: List[Dict[str, Any]] = []
        self._runner: Optional[Runner] = None

    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "job": json.loads(json.dumps(self.job)),
                # estado ao vivo vem do ARQUIVO: assim a barra mostra também
                # rodada disparada por CLI/cron, não só a que o painel iniciou
                "live": read_live(self.base_settings.state_dir),
                "recent": list(self.recent[-60:]),
                "account": mask_email(self.credentials.email),
                "channels": [
                    {
                        "key": channel.key,
                        "name": channel.name,
                        "integration_id": channel.integration_id,
                        "note": channel.note,
                    }
                    for channel in CHANNELS.values()
                    if channel.key != "bling"
                ],
                "speeds": {key: value["label"] for key, value in SPEEDS.items()},
                "defaults": {"low_stock_max": self.base_settings.low_stock_max, "speed": "normal"},
            }

    # ------------------------------------------------------------------ #
    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def publish(self, event: Dict[str, Any]) -> None:
        payload = {**event, "ts": _now()}
        with self._lock:
            if event.get("type") in ("info", "item_result", "channel_done", "run_done", "error"):
                self.recent.append(payload)
                if len(self.recent) > 200:
                    self.recent = self.recent[-200:]
            subscribers = list(self._subscribers)
        for channel in subscribers:
            try:
                channel.put_nowait(payload)
            except queue.Full:
                pass

    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        with self._lock:
            return self.job["status"] == "running"

    def preview(self, channel_key: str, mode: str) -> Dict[str, Any]:
        """Contagens read-only: catálogo, estoque baixo e desatualizados."""
        channel = CHANNELS.get(channel_key)
        if channel is None:
            raise ValueError(f"canal desconhecido: {channel_key}")
        settings = self.base_settings
        transport = HttpTransport(
            settings.api_base,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
            backoff=settings.retry_backoff,
            default_retry_after=settings.default_retry_after,
        )
        tokens = TokenProvider(transport, self.credentials)
        api = ApiClient(transport, tokens, read_attempts=2, write_attempts=1)
        started = time.time()
        try:
            me = api.get("/me")
            user_id = None
            if isinstance(me.data, dict):
                user_id = me.data.get("id") or me.data.get("userId")

            def count(extra: Dict[str, str]) -> Optional[int]:
                params = {
                    "integrationId": channel.integration_id,
                    "userId": user_id,
                    "situation": "mapped",
                    "limit": 1,
                    "offset": 0,
                    **extra,
                }
                response = api.get("/my-items", params=params)
                return extract_total(response.data) if response.data is not None else None

            total = count({})
            baixo = count({"lowStock": "true"})
            desatualizados = count({"outdatedStock": "true"})
            baixo_desatualizado = count({"lowStock": "true", "outdatedStock": "true"})
            selecionado = total if mode == "all" else baixo
            return {
                "ok": True,
                "channel": channel.key,
                "name": channel.name,
                "integration_id": channel.integration_id,
                "account": mask_email(self.credentials.email),
                "user_id": user_id,
                "mapped_total": total,
                "low_stock": baixo,
                "outdated": desatualizados,
                "low_outdated": baixo_desatualizado,
                "selected": selecionado,
                "seconds": round(time.time() - started, 1),
            }
        finally:
            transport.close()

    # ------------------------------------------------------------------ #
    def start(
        self,
        channel_key: str,
        *,
        channels: Optional[List[str]] = None,
        parallel: bool = False,
        mode: str = "low",
        dry_run: bool = True,
        low_stock_max: int = 10,
        speed: str = "normal",
        workers: Optional[int] = None,
        delay: Optional[float] = None,
        max_items: int = 0,
    ) -> Dict[str, Any]:
        if self.running:
            raise RuntimeError("já existe uma sincronização em andamento")

        # um canal, uma lista de canais (os 3 de uma vez) ou nada
        escolhidos: List[Channel] = []
        for key in channels or ([channel_key] if channel_key else []):
            canal = CHANNELS.get(str(key))
            if canal is None:
                raise ValueError(f"canal desconhecido: {key}")
            if canal.key != "bling":
                escolhidos.append(canal)
        if not escolhidos:
            raise ValueError("nenhum canal escolhido")
        channel = escolhidos[0]

        preset = speed_preset(speed)
        settings = replace(
            self.base_settings,
            order=tuple(escolhidos),
            parallel_channels=bool(parallel) and len(escolhidos) > 1,
            all_items=mode == "all",
            dry_run=bool(dry_run),
            low_stock_max=max(1, int(low_stock_max)),
            workers=int(workers or preset["workers"]),
            delay=float(preset["delay"] if delay is None else delay),
            verify_samples=0,
            resume=False,
            max_items=max(0, int(max_items or 0)),
        )
        settings.ensure_dirs()

        lock = RunLock(settings.lock_path)
        if not lock.acquire():
            owner = read_owner(settings.lock_path)
            raise RuntimeError(
                "já existe uma sincronização em andamento" + (f" (pid {owner})" if owner else "")
            )

        with self._lock:
            self.job = {
                "status": "running",
                "channel": channel.key,
                "channels": [c.key for c in escolhidos],
                "parallel": settings.parallel_channels,
                "mode": "dry-run" if dry_run else mode,
                "dry_run": bool(dry_run),
                "started_at": _now(),
                "done": 0,
                "total": 0,
                "counters": {"ok": 0, "partial": 0, "skipped": 0, "error": 0, "timeout": 0},
                "sent_quantity": 0,
                "eta_s": None,
                "rate": 0,
                "message": "conectando…",
                "summary": None,
            }
        rotulo = channel.name if len(escolhidos) == 1 else f"{len(escolhidos)} canais em paralelo"
        self.publish(
            {
                "type": "info",
                "message": f"{rotulo}: iniciando"
                + (" (simulação)" if dry_run else "")
                + f" · envios um por vez, {settings.delay:g}s de intervalo"
                + (f" · limitado a {settings.max_items}" if settings.max_items else ""),
            }
        )

        def work() -> None:
            runner: Optional[Runner] = None
            try:
                runner = Runner(settings, self.credentials, logger=self.log, on_event=self._on_event)
                with self._lock:
                    self._runner = runner
                result = runner.run()
                with self._lock:
                    channel_result = result.channels[0].to_dict() if result.channels else None
                    self.job["status"] = "error" if result.fatal_kind else "done"
                    self.job["summary"] = channel_result
                    if result.fatal:
                        self.job["message"] = result.fatal
                    elif channel_result is None:
                        self.job["message"] = "nada a fazer"
                    elif channel_result.get("status") == "dry-run":
                        self.job["message"] = (
                            f"simulação: {channel_result.get('selected', 0)} anúncio(s) seriam atualizados"
                            f" ({channel_result.get('mapped_total', 0)} no catálogo) — nada foi enviado"
                        )
                    else:
                        contagem = channel_result["counts"]
                        enviado = channel_result.get("sent_quantity") or 0
                        self.job["message"] = (
                            f"{contagem.get('ok', 0)} ok · {contagem.get('skipped', 0)} pulados · "
                            f"{contagem.get('error', 0)} erros"
                            + (f" · {enviado} unidades enviadas" if enviado else "")
                        )
                    if channel_result:
                        # números finais (o último evento de progresso pode ter vindo antes do fim)
                        contagem = channel_result["counts"]
                        self.job["counters"] = {
                            "ok": contagem.get("ok", 0),
                            "partial": contagem.get("partial", 0),
                            "skipped": contagem.get("skipped", 0),
                            "error": contagem.get("error", 0),
                            "timeout": contagem.get("timeout", 0),
                        }
                        self.job["sent_quantity"] = channel_result.get("sent_quantity") or 0
                        self.job["done"] = sum(self.job["counters"].values())
                        self.job["total"] = channel_result.get("selected") or self.job["total"]
                        self.job["eta_s"] = None
                self.publish(
                    {
                        "type": "run_done",
                        "message": self.job["message"],
                        "status": self.job["status"],
                        "summary": channel_result,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.log.exception("falha na rodada do painel")
                with self._lock:
                    self.job["status"] = "error"
                    self.job["message"] = f"{type(exc).__name__}: {exc}"
                self.publish({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                with self._lock:
                    self._runner = None
                lock.release()

        threading.Thread(target=work, name="wedrop-web-run", daemon=True).start()
        return self.snapshot()["job"]

    def cancel(self) -> bool:
        with self._lock:
            runner = self._runner
        if runner is None:
            return False
        runner.stop()
        self.publish({"type": "info", "message": "cancelamento pedido — encerrando com segurança"})
        return True

    # ------------------------------------------------------------------ #
    def _on_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        with self._lock:
            if kind == "channel_start":
                self.job["total"] = int(event.get("selected") or 0)
                self.job["message"] = (
                    f"{event.get('mapped_total')} no catálogo · {event.get('selected')} selecionados"
                    + (f" · {event.get('blocked')} bloqueados (pulados)" if event.get("blocked") else "")
                )
            elif kind == "rate_gate":
                pode = event.get("can_make_requests")
                self.job["gate"] = {
                    "can_make_requests": pode,
                    "request_count": event.get("request_count"),
                    "consecutive_429": event.get("consecutive_429"),
                    "in_backoff": event.get("in_backoff"),
                }
                if pode is False:
                    self.job["message"] = "rate limit do canal fechado — aguardando liberar"
            elif kind == "progress":
                self.job["done"] = int(event.get("done") or 0)
                self.job["total"] = int(event.get("total") or self.job["total"])
                self.job["counters"] = {
                    "ok": int(event.get("ok") or 0),
                    "partial": int(event.get("partial") or 0),
                    "skipped": int(event.get("skipped") or 0),
                    "error": int(event.get("errors") or 0),
                    "timeout": int(event.get("timeouts") or 0),
                }
                self.job["sent_quantity"] = int(event.get("sent_quantity") or 0)
                self.job["rate"] = float(event.get("rate") or 0)
                self.job["eta_s"] = event.get("eta_s")
            elif kind == "channel_done":
                self.job["summary"] = event.get("summary")
                self.job["message"] = "finalizando…"
        self.publish(event)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "wedrop-sync"
    app: App  # injetado

    def log_message(self, *args):  # silêncio no stdout
        pass

    # ---- helpers ----
    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    # ---- rotas ----
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        try:
            if path in ("/", "/index.html"):
                return self._html()
            if path == "/api/status":
                return self._json(200, self.app.snapshot())
            if path == "/api/preview":
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                result = self.app.preview(params.get("channel", "shopee"), params.get("mode", "low"))
                return self._json(200, result)
            if path == "/api/events":
                return self._events()
            return self._json(404, {"ok": False, "error": "rota desconhecida"})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/api/run":
                body = self._body()
                canais = body.get("channels")
                job = self.app.start(
                    str(body.get("channel", "")),
                    channels=[str(c) for c in canais] if isinstance(canais, list) else None,
                    parallel=bool(body.get("parallel")),
                    mode=str(body.get("mode", "low")),
                    dry_run=bool(body.get("dryRun", True)),
                    low_stock_max=int(body.get("lowStockMax") or 10),
                    speed=str(body.get("speed", "normal")),
                    max_items=int(body.get("maxItems") or 0),
                )
                return self._json(202, {"ok": True, "job": job})
            if path == "/api/cancel":
                return self._json(202, {"ok": self.app.cancel()})
            return self._json(404, {"ok": False, "error": "rota desconhecida"})
        except RuntimeError as exc:
            return self._json(409, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # ---- SSE ----
    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        channel = self.app.subscribe()
        try:
            self._write_sse({"type": "hello", "job": self.app.snapshot()["job"]})
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_sse(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.unsubscribe(channel)

    def _write_sse(self, payload: Dict[str, Any]) -> None:
        data = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()

    # ---- página ----
    def _html(self) -> None:
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebServer:
    def __init__(self, settings: Settings, credentials: Credentials, *, host: str = "127.0.0.1", port: int = 8790, logger=None) -> None:
        self.app = App(settings, credentials, logger=logger)
        handler = type("BoundHandler", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.host = host
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estoque WeDrop</title>
<style>
  :root{
    --paper:#f4f1e9; --card:#fffdf7; --ink:#1b1a17; --dim:#6d675c; --line:#d9d3c4;
    --go:#0f7a4d; --go-soft:#e2f0e8; --warn:#a86410; --warn-soft:#fbeee0; --stop:#a5322a;
    --mono:ui-monospace,SFMono-Regular,Consolas,"Cascadia Mono",monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --shadow:4px 4px 0 var(--ink);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--sans);
       background-image:radial-gradient(var(--line) 1px,transparent 1px);background-size:22px 22px;}
  .wrap{max-width:940px;margin:0 auto;padding:34px 20px 70px;display:flex;flex-direction:column;gap:18px}
  header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
  .kicker{font:600 11px var(--mono);letter-spacing:.22em;text-transform:uppercase;color:var(--dim)}
  h1{margin:2px 0 0;font-size:clamp(26px,4vw,38px);letter-spacing:-.03em;line-height:1}
  .who{font:12px var(--mono);color:var(--dim);text-align:right}
  .who b{color:var(--ink)}
  .board{background:var(--card);border:2px solid var(--ink);border-radius:2px;box-shadow:var(--shadow)}
  .control{display:flex;flex-wrap:wrap;gap:14px;align-items:center;padding:14px 16px;border-bottom:2px solid var(--ink)}
  .control label{font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--dim)}
  select,input[type=number]{font:600 14px var(--mono);padding:7px 9px;border:2px solid var(--ink);
    border-radius:2px;background:var(--paper);color:var(--ink)}
  input[type=number]{width:74px}
  .switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
  .switch input{appearance:none;width:44px;height:24px;border:2px solid var(--ink);border-radius:2px;background:var(--paper);position:relative;cursor:pointer}
  .switch input::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;background:var(--ink);transition:transform .12s ease}
  .switch input:checked{background:var(--go-soft);border-color:var(--go)}
  .switch input:checked::after{transform:translateX(20px);background:var(--go)}
  .row{display:grid;grid-template-columns:1fr auto;gap:14px;align-items:center;padding:16px;border-bottom:1px solid var(--line)}
  .row:last-of-type{border-bottom:none}
  .row.busy{background:var(--go-soft)}
  .row.name{display:flex;flex-direction:column;gap:3px}
  .name h2{margin:0;font-size:19px;letter-spacing:-.01em}
  .name .id{font:11px var(--mono);color:var(--dim)}
  .stats{display:flex;gap:18px;margin-top:6px;flex-wrap:wrap}
  .stat{font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
  .stat b{display:block;font:600 17px var(--mono);letter-spacing:0;color:var(--ink);text-transform:none}
  .stat.hot b{color:var(--warn)}
  button.go{font:700 14px var(--sans);letter-spacing:.02em;padding:13px 20px;border:2px solid var(--ink);
    border-radius:2px;background:var(--card);color:var(--ink);cursor:pointer;box-shadow:3px 3px 0 var(--ink);
    transition:transform .05s ease,box-shadow .05s ease,background .12s ease;white-space:nowrap}
  button.go:hover:not(:disabled){background:var(--go);color:#fff}
  button.go:active:not(:disabled){transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
  button.go:disabled{opacity:.42;cursor:not-allowed;box-shadow:none}
  button.go.danger{background:var(--stop);color:#fff}
  .prog{margin-top:10px;display:none}
  .row.busy .prog{display:block}
  .bar{height:9px;border:2px solid var(--ink);background:var(--paper);position:relative;overflow:hidden}
  .bar span{display:block;height:100%;width:0;background:var(--go);transition:width .3s ease}
  .prog .line{font:12px var(--mono);color:var(--dim);margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
  .prog .line b{color:var(--ink)}
  .log{background:var(--card);border:2px solid var(--ink);border-radius:2px;box-shadow:var(--shadow);padding:0}
  .log h3{margin:0;padding:10px 16px;border-bottom:2px solid var(--ink);font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.14em;color:var(--dim)}
  .log ol{list-style:none;margin:0;padding:10px 16px;max-height:230px;overflow:auto;font:12px var(--mono);display:flex;flex-direction:column;gap:4px}
  .log li{display:flex;gap:10px;color:var(--dim)}
  .log li time{color:#a09a8c;flex:none}
  .log li.err{color:var(--stop)}
  .log li.ok{color:var(--go)}
  .foot{font:11px var(--mono);color:var(--dim);text-align:center}
  .pill{display:inline-block;font:600 11px var(--mono);text-transform:uppercase;letter-spacing:.1em;
    padding:4px 9px;border:2px solid var(--ink);border-radius:2px;background:var(--card)}
  .pill.run{background:var(--go-soft);border-color:var(--go);color:var(--go)}
  .pill.err{background:var(--warn-soft);border-color:var(--warn);color:var(--warn)}
  @media (max-width:640px){ .row{grid-template-columns:1fr} button.go{width:100%} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <div class="kicker">Sincronizador de estoque</div>
      <h1>WeDrop</h1>
    </div>
    <div class="who">
      <div id="pill" class="pill">pronto</div>
      <div style="margin-top:6px">conta <b id="account">—</b></div>
    </div>
  </header>

  <section class="board">
    <div class="control">
      <label for="mode">Critério</label>
      <select id="mode">
        <option value="low">Estoque baixo ≤</option>
        <option value="all">Tudo (todos os mapeados)</option>
      </select>
      <input id="limit" type="number" min="1" step="1" value="10" />
      <label for="speed">Ritmo</label>
      <select id="speed"><option value="cuidadoso" selected>cuidadoso · 5s</option><option value="normal">padrão · 2s</option><option value="rapido">agressivo · 0,5s</option></select>
      <label class="switch" for="dry"><input id="dry" type="checkbox" checked /><span>só simular</span></label>
      <label for="maxitens">Máx. anúncios</label>
      <input id="maxitens" type="number" min="0" step="1" value="0" title="0 = atualiza todos os selecionados" />
      <button id="run-all" class="go" style="margin-left:auto">Sincronizar os 3</button>
      <button id="cancel" class="go danger" disabled>Cancelar</button>
    </div>
    <div id="rows"></div>
  </section>

  <section class="log">
    <h3>Últimos eventos</h3>
    <ol id="log"></ol>
  </section>

  <div class="foot">painel local · a rodada fala direto com a API do WeDrop · nada trafega por fora</div>
</div>

<script>
const $ = (s) => document.querySelector(s);
let state = null;
let busy = false;

function el(tag, props = {}, ...kids) {
  const node = Object.assign(document.createElement(tag), props);
  kids.forEach((kid) => node.append(kid));
  return node;
}

function money(n) { return n == null ? '—' : String(n); }

function renderRows() {
  const host = $('#rows');
  host.innerHTML = '';
  (state.channels || []).forEach((channel) => {
    const isBusy = busy && state.job.channel === channel.key;
    const row = el('div', { className: 'row' + (isBusy ? ' busy' : '') });

    const name = el('div', { className: 'row name' });
    name.append(
      el('h2', { textContent: channel.name }),
      el('div', { className: 'id', textContent: 'integrationId ' + channel.integration_id }),
    );
    const stats = el('div', { className: 'stats' });
    const dados = (state.preview || {})[channel.key];
    const addStat = (rotulo, valor, hot) => {
      const box = el('div', { className: 'stat' + (hot ? ' hot' : '') });
      box.append(document.createTextNode(rotulo), el('b', { textContent: money(valor) }));
      stats.append(box);
    };
    addStat('catálogo', dados ? dados.mapped_total : null);
    addStat('estoque baixo', dados ? dados.low_stock : null, true);
    addStat('desatualizados', dados ? dados.outdated : null);
    addStat('na seleção', dados ? dados.selected : null);
    name.append(stats);

    const prog = el('div', { className: 'prog' });
    const bar = el('div', { className: 'bar' });
    bar.append(el('span', { id: 'bar-' + channel.key }));
    const line = el('div', { className: 'line', id: 'line-' + channel.key });
    prog.append(bar, line);
    name.append(prog);

    const btn = el('button', {
      className: 'go',
      textContent: busy && state.job.channel === channel.key ? 'Em andamento…' : (($('#dry').checked) ? 'Simular' : 'Sincronizar'),
    });
    btn.disabled = busy;
    btn.addEventListener('click', () => start(channel.key));
    row.append(name, btn);
    host.append(row);
  });
  renderProgress();
}

function renderProgress() {
  // A barra é desenhada a partir do estado AO VIVO (arquivo), então funciona
  // para rodada do painel, do CLI, do cron, e para os 3 canais em paralelo.
  const live = state && state.live;
  const canais = (live && live.channels) || {};
  let algum = false;
  Object.keys(canais).forEach((key) => {
    const dados = canais[key];
    const bar = document.getElementById('bar-' + key);
    const line = document.getElementById('line-' + key);
    if (!bar || !line) return;
    const prog = bar.closest('.prog');
    const row = bar.closest('.row');
    const ativo = dados.done < dados.selected || dados.status === 'running';
    if (prog) prog.style.display = (ativo || busy) ? 'block' : 'none';
    if (row) row.classList.toggle('busy', ativo);
    algum = algum || ativo;
    const pct = dados.selected ? Math.min(100, (dados.done / dados.selected) * 100) : 0;
    bar.style.width = pct.toFixed(1) + '%';
    line.innerHTML = `<span><b>${dados.done||0}</b>/${dados.selected||0}</span>` +
      `<span>ok <b>${dados.ok||0}</b></span>` +
      `<span>pulados <b>${dados.skipped||0}</b></span>` +
      `<span>erros <b>${dados.error||0}</b></span>` +
      (dados.sent_quantity ? `<span>enviado <b>${dados.sent_quantity}</b></span>` : '');
  });
  if (live && live.running) {
    const t = live.totals || {};
    setPill(`rodando ${t.done||0}/${t.selected||0}`, 'run');
    $('#cancel').disabled = false;
  } else if (!busy && live) {
    setPill('pronto');
  }
  return algum;
}

function fmtEta(s) {
  s = Math.round(s);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'min';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0');
}

function setPill(text, kind) {
  const pill = $('#pill');
  pill.textContent = text;
  pill.className = 'pill' + (kind ? ' ' + kind : '');
}

function addLog(message, kind) {
  const item = el('li', { className: kind || '' });
  item.append(el('time', { textContent: new Date().toLocaleTimeString('pt-BR') }), document.createTextNode(message));
  const log = $('#log');
  log.prepend(item);
  while (log.children.length > 60) log.lastChild.remove();
}

async function loadStatus(withPreviews = true) {
  const response = await fetch('/api/status');
  state = await response.json();
  busy = state.job.status === 'running';
  const externa = !!(state.live && state.live.running) && !busy;
  $('#account').textContent = state.account;
  $('#cancel').disabled = !(busy || externa);
  if (externa) setPill('rodando (fora do painel)', 'run');
  else if (!busy) setPill(state.job.status === 'error' ? 'erro' : 'pronto', state.job.status === 'error' ? 'err' : '');
  if (!state.preview) state.preview = {};
  renderRows();
  renderProgress();
  if (withPreviews) loadPreviews();
}

async function loadPreviews() {
  if (!state || !state.channels) return;
  const mode = $('#mode').value;
  for (const channel of state.channels) {
    try {
      const response = await fetch(`/api/preview?channel=${channel.key}&mode=${mode}`);
      const data = await response.json();
      if (data.ok) (state.preview = state.preview || {})[channel.key] = data;
    } catch (e) { /* preview é cortesia */ }
  }
  renderRows();
}

async function start(channelKey, todos = false) {
  const body = {
    channel: todos ? '' : channelKey,
    channels: todos ? (state.channels || []).map((c) => c.key) : null,
    parallel: !!todos,
    mode: $('#mode').value,
    dryRun: $('#dry').checked,
    lowStockMax: Number($('#limit').value || 10),
    speed: $('#speed').value,
    maxItems: Number($('#maxitens').value || 0),
  };
  const response = await fetch('/api/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) { addLog(data.error || 'não consegui iniciar', 'err'); setPill('erro', 'err'); return; }
  busy = true;
  state.job = data.job;
  $('#cancel').disabled = false;
  setPill('rodando', 'run');
  const alvo = todos
    ? `${(state.channels || []).length} canais`
    : (state.channels.find((c) => c.key === channelKey) || {}).name;
  addLog(`${alvo}: iniciando…`, 'ok');
  renderRows();
}

$('#run-all').addEventListener('click', () => start(null, true));

$('#cancel').addEventListener('click', async () => {
  await fetch('/api/cancel', { method: 'POST' });
  addLog('cancelamento pedido', 'err');
});

$('#mode').addEventListener('change', () => {
  $('#limit').disabled = $('#mode').value === 'all';
  loadPreviews();
});
$('#limit').addEventListener('change', loadPreviews);
$('#dry').addEventListener('change', () => renderRows());

const source = new EventSource('/api/events');
source.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'hello') return;
  busy = data.type !== 'run_done' && data.type !== 'error' && state && state.job.status === 'running';
  if (['progress', 'channel_start', 'channel_done', 'run_done', 'item_result', 'info', 'error'].includes(data.type) && state) {
    if (data.type === 'progress') {
      state.job.done = data.done; state.job.total = data.total;
      state.job.counters = { ok: data.ok, partial: data.partial, skipped: data.skipped, error: data.errors, timeout: data.timeouts };
      state.job.sent_quantity = data.sent_quantity; state.job.eta_s = data.eta_s;
      renderProgress();
      return;
    }
    if (data.type === 'channel_start') {
      state.job.total = data.selected; state.job.done = 0;
      addLog(`${data.name}: ${data.selected} na seleção (${data.mapped_total} mapeados)`);
      renderRows();
      return;
    }
    if (data.type === 'item_result') {
      addLog(`${data.item_id} · ${data.outcome}${data.message ? ' · ' + data.message : ''}`, data.outcome === 'error' ? 'err' : '');
      return;
    }
    if (data.type === 'run_done') {
      busy = false;
      setPill(data.status === 'error' ? 'erro' : 'pronto', data.status === 'error' ? 'err' : '');
      addLog('concluído: ' + (data.message || ''), 'ok');
      $('#cancel').disabled = true;
      loadStatus();
      return;
    }
    if (data.message) addLog(data.message, data.type === 'error' ? 'err' : '');
  }
};

loadStatus();
// barra ao vivo: 3s enquanto houver rodada (inclusive disparada fora do painel)
setInterval(() => {
  const ativa = (state && state.live && state.live.running) || busy;
  if (ativa) loadStatus(false);
}, 3000);
setInterval(() => { if (!busy) loadStatus(true); }, 60000);
</script>
</body>
</html>
"""


