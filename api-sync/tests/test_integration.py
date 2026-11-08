"""Testes de integraÃ§Ã£o: transporte HTTP, Runner, polÃ­tica de alerta e CLI.

Tudo roda contra `tests/mockapi.py` (servidor HTTP real, em memória): nenhuma
requisição sai para a internet e nenhuma credencial de verdade é usada.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.mockapi import FakeWeDrop

from wedrop_sync import cli, config, report
from wedrop_sync.auth import TokenProvider
from wedrop_sync.client import ApiClient, HttpTransport
from wedrop_sync.lock import RunLock
from wedrop_sync.runner import ChannelResult, RunResult, Runner


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages = []

    def emit(self, record) -> None:
        self.messages.append(record.getMessage())


class FakeApiTestCase(unittest.TestCase):
    """Base: sobe a API falsa, cria um `home` temporário e captura o log."""

    workers = 2

    def setUp(self) -> None:
        self.fake = FakeWeDrop()
        self.api_base = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.home = Path(tempfile.mkdtemp(prefix="wedrop-it-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.logger = logging.getLogger(f"wedrop.test.{id(self)}")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.capture = CaptureHandler()
        self.logger.addHandler(self.capture)

    # ------------------------------------------------------------------ #
    def settings(self, **overrides):
        args = {
            "home": str(self.home),
            "api_base": self.api_base,
            "workers": self.workers,
            "delay": 0.0,
            "verify_samples": 0,
            "only": "shopee,ml1,ml2",
        }
        args.update(overrides)
        settings = config.load_settings(args, env={})
        settings.ensure_dirs()
        return settings

    def credentials(self):
        return config.Credentials("marco@loja.com", "senha-de-teste")

    def runner(self, **overrides) -> Runner:
        return Runner(self.settings(**overrides), self.credentials(), logger=self.logger)

    def posted_items(self):
        return [
            body.get("itemId")
            for method, path, body in self.fake.paths
            if method == "POST" and str(path).endswith("/update-stock")
        ]

    def messages(self):
        return self.capture.messages


# --------------------------------------------------------------------------- #
# Transporte / cliente
# --------------------------------------------------------------------------- #
class ClientTests(FakeApiTestCase):
    def client(self, **overrides):
        settings = self.settings(**overrides)
        transport = HttpTransport(
            settings.api_base,
            connect_timeout=2.0,
            read_timeout=settings.read_timeout,
            backoff=0.01,
            default_retry_after=settings.default_retry_after,
        )
        self.addCleanup(transport.close)
        return (
            ApiClient(
                transport,
                TokenProvider(transport, self.credentials()),
                read_attempts=settings.read_attempts,
                write_attempts=settings.update_attempts,
            ),
            transport,
        )

    def test_get_ok(self):
        api, _ = self.client()
        response = api.get("/me")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.data["userId"], 10000)

    def test_leitura_repete_5xx(self):
        self.fake.fail_read_queue["/my-items"] = [500]
        api, _ = self.client()
        response = api.get("/my-items", params={"integrationId": 10001})
        self.assertEqual(response.status, 200)

    def test_escrita_nao_repete_5xx(self):
        self.fake.behavior("shp-000", "http500")
        api, _ = self.client()
        response = api.post(
            "/my-items/update-stock", {"itemId": "shp-000", "integrationId": 10001}, attempts=3
        )
        self.assertEqual(response.status, 500)
        self.assertEqual(self.fake.item_calls["shp-000"], 1, "5xx na escrita não pode repetir")

    def test_429_espera_e_repete(self):
        self.fake.rate_limit_queue = ["http429"]
        api, transport = self.client()
        with mock.patch("wedrop_sync.client.time.sleep"):
            response = api.post("/my-items/update-stock", {"itemId": "shp-000", "integrationId": 10001})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.fake.item_calls["shp-000"], 2)
        self.assertGreaterEqual(transport.rate_limit_hits, 1)

    def test_401_renova_token_e_repete(self):
        self.fake.reject_once["/my-items"] = 1
        api, _ = self.client()
        response = api.get("/my-items", params={"integrationId": 10001})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.fake.auth_calls, 2)

    def test_conexao_e_token_reaproveitados(self):
        api, transport = self.client()
        for _ in range(3):
            api.get("/me")
        # 1 login + 3 leituras: o token é reaproveitado, não há re-login por chamada
        self.assertEqual(transport.requests_made, 4)
        self.assertEqual(self.fake.auth_calls, 1)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class RunnerTests(FakeApiTestCase):
    def test_rodada_normal(self):
        run = self.runner().run()
        self.assertEqual([c.key for c in run.channels], ["shopee", "ml1", "ml2"])
        shopee = run.channel("shopee")
        self.assertEqual(shopee.status, "ok")
        self.assertEqual(shopee.mapped_total, 12)
        self.assertEqual(shopee.selected, 6)
        self.assertEqual(shopee.ok, 6)
        self.assertEqual(run.total_ok, 6 + 4 + 5)
        self.assertEqual(self.fake.update_calls, 15)
        self.assertEqual(self.fake.auth_calls, 1)
        self.assertTrue(any(line.startswith("SUMMARY shopee") for line in self.messages()))
        self.assertFalse(run.fatal)

    def test_blings_fora_do_fluxo(self):
        run = self.runner().run()
        self.assertNotIn("bling", [c.key for c in run.channels])
        self.assertNotIn(10004, self.fake.items_by_channel.get(10004, []) or [])

    def test_desatualizados_vem_primeiro(self):
        self.workers = 1
        self.runner(workers=1).run()
        posted = self.posted_items()
        # Shopee: estoque baixo = shp-000..005; desatualizados = shp-003..005
        self.assertEqual(posted[:3], ["shp-003", "shp-004", "shp-005"])

    def test_modo_completo(self):
        run = self.runner(all_items=True).run()
        self.assertEqual(run.channel("shopee").selected, 12)
        self.assertEqual(run.mode, "full")

    def test_dry_run_nao_envia_nada(self):
        run = self.runner(dry_run=True).run()
        self.assertEqual(self.fake.update_calls, 0)
        self.assertTrue(all(channel.status == "dry-run" for channel in run.channels))
        self.assertEqual(run.mode, "dry-run")

    def test_orfao_e_revisao_nao_alertam(self):
        for index in range(3):
            self.fake.behavior(f"shp-{index:03d}", "orphan")
        for index in range(3, 6):
            self.fake.behavior(f"shp-{index:03d}", "under_review")
        settings = self.settings()
        run = self.runner().run()
        shopee = run.channel("shopee")
        self.assertEqual(shopee.skipped, 6)
        self.assertEqual(shopee.errors, 0)
        self.assertEqual(shopee.reasons["orphan"], 3)
        self.assertEqual(shopee.reasons["channel_status"], 3)
        self.assertEqual(report.decide_alerts(run, settings), [])

    def test_estado_ao_vivo_fica_em_arquivo(self):
        """A barra do painel lê este arquivo: vale para rodada de CLI/cron também."""
        from wedrop_sync.state import read_live

        self.runner(only="shopee").run()
        live = read_live(self.settings().state_dir)
        self.assertIsNotNone(live)
        self.assertFalse(live["running"])
        self.assertEqual(live["channels"]["shopee"]["ok"], 6)
        self.assertEqual(live["totals"]["ok"], 6)
        self.assertEqual(live["totals"]["selected"], 6)

    def test_modo_paralelo_roda_os_tres_canais(self):
        run = self.runner(parallel_channels=True, only="shopee,ml1,ml2").run()
        self.assertEqual(sorted(c.key for c in run.channels), ["ml1", "ml2", "shopee"])
        self.assertEqual(run.total_ok, 6 + 4 + 5)
        self.assertEqual(self.fake.update_calls, 15)
        self.assertFalse(run.fatal)

    def test_modo_paralelo_com_canal_sem_autorizacao_nao_derruba_os_outros(self):
        for index in range(6):
            self.fake.behavior(f"shp-{index:03d}", "auth_loss")
        settings = self.settings(parallel_channels=True, breaker=3)
        run = Runner(settings, self.credentials(), logger=self.logger).run()
        self.assertEqual(run.channel("shopee").status, "aborted")
        self.assertGreater(run.channel("ml1").ok, 0, "o outro canal tem que terminar normalmente")

    def test_anuncio_bloqueado_nao_gasta_requisicao(self):
        """O WeDrop já marca combo/revisão com stockSyncState=bloqueado: pula sem chamar a API."""
        for item in self.fake.items_by_channel[10001]:
            item["stockSyncState"] = "bloqueado"
            item["stockSyncReason"] = "Combo: o canal calcula o estoque pelos produtos que o compõem"
        run = self.runner(only="shopee").run()
        shopee = run.channel("shopee")
        self.assertEqual(self.fake.update_calls, 0, "nada deveria ser enviado")
        self.assertEqual(shopee.ok, 0)
        self.assertEqual(shopee.skipped, shopee.selected)
        self.assertEqual(shopee.errors, 0)
        self.assertEqual(shopee.reasons["channel_status"], shopee.selected)
        self.assertEqual(shopee.blocked_skipped, shopee.selected)

    def test_verificacao_de_residuo_confirma_efeito(self):
        run = self.runner(verify_samples=10).run()
        shopee = run.channel("shopee")
        self.assertEqual(shopee.status, "ok")
        self.assertEqual(
            shopee.residual_outdated,
            0,
            "depois de atualizar, nenhum item relido deve continuar marcado como desatualizado",
        )

    def test_erro_isolado_nao_derruba_rodada(self):
        self.fake.behavior("shp-000", "plain_error")
        self.fake.behavior("shp-001", "http500")
        settings = self.settings()
        run = self.runner().run()
        shopee = run.channel("shopee")
        self.assertEqual(shopee.errors, 2)
        self.assertEqual(shopee.ok, 4)
        self.assertIn("ml1", [c.key for c in run.channels])
        self.assertEqual(report.decide_alerts(run, settings), [])

    def test_timeout_conta_e_segue(self):
        self.fake.behavior("shp-000", "timeout")
        run = self.runner(read_timeout=0.2, update_attempts=1).run()
        shopee = run.channel("shopee")
        self.assertEqual(shopee.timeouts, 1)
        self.assertEqual(shopee.ok, 5)

    def test_circuit_breaker_aborta_e_segue_para_o_proximo_canal(self):
        for index in range(6):
            self.fake.behavior(f"shp-{index:03d}", "auth_loss")
        settings = self.settings(breaker=3)
        run = Runner(settings, self.credentials(), logger=self.logger).run()
        shopee = run.channel("shopee")
        self.assertEqual(shopee.status, "aborted")
        self.assertIn("autorização", shopee.aborted_reason)
        self.assertGreaterEqual(shopee.reasons["auth_loss"], 3)
        self.assertIn("ml1", [c.key for c in run.channels], "a rodada tem que seguir para o próximo canal")
        alerts = report.decide_alerts(run, settings)
        self.assertTrue(any(alert.kind == "authorization_lost" for alert in alerts))

    def test_resume_pula_itens_ja_enviados(self):
        self.runner().run()
        first_round = self.fake.update_calls
        run = self.runner(resume=True).run()
        self.assertEqual(self.fake.update_calls, first_round, "resume não pode reenviar nada")
        self.assertEqual(run.total_processed, 0)

    def test_leitura_com_5xx_se_recupera(self):
        self.fake.fail_read_queue["/my-items"] = [502]
        run = self.runner().run()
        self.assertGreater(run.total_ok, 0)
        self.assertEqual(run.channel("shopee").status, "ok")

    def test_senha_nao_aparece_no_log(self):
        self.runner().run()
        joined = "\n".join(self.messages())
        self.assertNotIn("senha-de-teste", joined)
        self.assertNotIn("marco@loja.com", joined)

    def test_falha_de_login_para_a_rodada(self):
        self.fake.reject_login = True
        run = self.runner().run()
        self.assertEqual(run.fatal_kind, "invalid_credentials")
        self.assertEqual(run.channels, [])
        self.assertEqual(self.fake.update_calls, 0)


# --------------------------------------------------------------------------- #
# Política de alerta
# --------------------------------------------------------------------------- #
class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="wedrop-report-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.settings = config.load_settings({"home": str(self.home)}, env={})

    @staticmethod
    def channel(errors=0, timeouts=0, processed=100, reasons=None, status="ok", aborted="", key="shopee"):
        result = ChannelResult(key=key, name=key.upper(), integration_id=1)
        result.status = status
        result.aborted_reason = aborted
        ok = max(0, processed - errors - timeouts)
        result.counts = {"ok": ok, "partial": 0, "skipped": 0, "error": errors, "timeout": timeouts}
        result.reasons.update(reasons or {})
        return result

    def test_ruido_de_catalogo_e_silencio(self):
        run = RunResult(channels=[self.channel(reasons={"orphan": 9, "channel_status": 4})])
        self.assertEqual(report.decide_alerts(run, self.settings), [])

    def test_pico_de_erro_alerta(self):
        run = RunResult(channels=[self.channel(errors=40, processed=100, reasons={"item_error": 40})])
        alerts = report.decide_alerts(run, self.settings)
        self.assertTrue(any(alert.kind == "error_spike" for alert in alerts))
        self.assertTrue(report.needs_human(alerts))

    def test_poucos_erros_nao_alertam(self):
        run = RunResult(channels=[self.channel(errors=5, processed=100, reasons={"item_error": 5})])
        self.assertEqual(report.decide_alerts(run, self.settings), [])

    def test_volume_alto_com_taxa_baixa_nao_alerta(self):
        run = RunResult(channels=[self.channel(errors=20, processed=1000, reasons={"item_error": 20})])
        self.assertEqual(report.decide_alerts(run, self.settings), [])

    def test_login_recusado_alerta(self):
        run = RunResult(fatal="senha inválida", fatal_kind="invalid_credentials")
        alerts = report.decide_alerts(run, self.settings)
        self.assertEqual(alerts[0].kind, "login_failed")
        self.assertEqual(alerts[0].severity, "fatal")

    def test_codigo_por_email_alerta(self):
        run = RunResult(fatal="pediu código", fatal_kind="email_code")
        alerts = report.decide_alerts(run, self.settings)
        self.assertEqual(alerts[0].kind, "email_code")

    def test_canal_abortado_alerta(self):
        run = RunResult(channels=[self.channel(status="aborted", aborted="muitos erros")])
        alerts = report.decide_alerts(run, self.settings)
        self.assertTrue(any(alert.kind == "channel_aborted" for alert in alerts))

    def test_mensagem_em_portugues(self):
        run = RunResult(channels=[self.channel(errors=40, processed=100, reasons={"auth_loss": 40})])
        alerts = report.decide_alerts(run, self.settings)
        text = report.format_alert_message(run, alerts)
        self.assertIn("precisa de atenção", text)
        self.assertIn("SHOPEE", text)

    def test_tendencia_de_piora(self):
        previous = {"channels": [{"counts": {"error": 4}}]}
        run = RunResult(channels=[self.channel(errors=40, processed=100, reasons={"item_error": 40})])
        alerts = report.decide_alerts(run, self.settings, previous=previous)
        self.assertTrue(any(alert.kind == "error_trend" for alert in alerts))

    def test_historico_formatado(self):
        records = [
            {
                "started_at": "2026-10-25T06:07:00-03:00",
                "duration_s": 5400,
                "mode": "normal",
                "channels": [{"key": "shopee", "counts": {"ok": 859, "error": 4}}],
            }
        ]
        text = report.format_history(records)
        self.assertIn("shopee:ok=859", text)


# --------------------------------------------------------------------------- #
# CLI (ponta a ponta)
# --------------------------------------------------------------------------- #
class CliTests(FakeApiTestCase):
    def env(self, **extra):
        base = {
            "WEDROP_API_BASE": self.api_base,
            "WEDROP_EMAIL": "marco@loja.com",
            "WEDROP_PASSWORD": "senha-de-teste",
        }
        base.update(extra)
        return base

    def run_cli(self, argv, **env):
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, self.env(**env), clear=False):
            with redirect_stdout(buffer):
                code = cli.main(argv)
        return code, buffer.getvalue()

    def run_argv(self, *extra):
        return ["run", "--home", str(self.home), "--workers", "2", "--delay", "0", "--verify-samples", "0", *extra]

    def test_rodada_grava_estado_log_e_historico(self):
        code, out = self.run_cli(self.run_argv())
        self.assertEqual(code, 0)
        self.assertIn("Rodada", out)
        self.assertTrue((self.home / "last-run.log").exists())
        self.assertIn("SUMMARY", (self.home / "last-run.log").read_text(encoding="utf-8"))

        record = json.loads((self.home / "state" / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["totals"]["ok"], 15)
        self.assertEqual([channel["key"] for channel in record["channels"]], ["shopee", "ml1", "ml2"])
        self.assertEqual(record["alerts"], [])

        self.run_cli(self.run_argv())
        self.assertTrue((self.home / "prev-run.log").exists(), "segunda rodada precisa rotacionar o log")
        history = (self.home / "state" / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(history), 2)

    def test_trava_ocupada_sai_sem_mexer_em_nada(self):
        lock = RunLock(self.home / ".sync.lock")
        self.assertTrue(lock.acquire())
        try:
            code, _ = self.run_cli(self.run_argv())
        finally:
            lock.release()
        self.assertEqual(code, 4)
        self.assertFalse((self.home / "last-run.log").exists())
        self.assertEqual(self.fake.update_calls, 0)

    def test_alerta_chega_no_arquivo_de_avisos(self):
        for index in range(6):
            self.fake.behavior(f"shp-{index:03d}", "auth_loss")
        avisos = self.home / "avisos.txt"
        code, _ = self.run_cli(
            self.run_argv("--notify-file", str(avisos), "--workers", "1"),
            WEDROP_BREAKER="3",
        )
        self.assertEqual(code, 2)
        self.assertTrue(avisos.exists())
        texto = avisos.read_text(encoding="utf-8")
        self.assertIn("precisa de atenção", texto)
        self.assertIn("perda de autorização", texto)

    def test_dry_run_nao_atualiza(self):
        code, _ = self.run_cli(self.run_argv("--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.update_calls, 0)

    def test_check_ok(self):
        code, out = self.run_cli(["check", "--home", str(self.home)])
        self.assertEqual(code, 0)
        self.assertIn("integrationId=10001", out)
        self.assertIn("userId: 10000", out)

    def test_check_detecta_integracao_ausente(self):
        self.fake.integrations = [(10001, "Shopee")]
        code, out = self.run_cli(["check", "--home", str(self.home)])
        self.assertEqual(code, 2)
        self.assertIn("!! ml1", out)

    def test_status_mostra_ultima_rodada(self):
        self.run_cli(self.run_argv())
        code, out = self.run_cli(["status", "--home", str(self.home)])
        self.assertEqual(code, 0)
        self.assertIn("shopee:ok=", out)

    def test_login_recusado_sai_com_codigo_de_autenticacao(self):
        self.fake.reject_login = True
        code, _ = self.run_cli(self.run_argv())
        self.assertEqual(code, 3)
        self.assertEqual(self.fake.update_calls, 0)


# --------------------------------------------------------------------------- #
# Painel (web.py): um botão por integração
# --------------------------------------------------------------------------- #
class WebTests(FakeApiTestCase):
    def servidor(self):
        from wedrop_sync.web import WebServer

        settings = self.settings()
        server = WebServer(settings, self.credentials(), host="127.0.0.1", port=0, logger=self.logger)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.stop)
        return server

    @staticmethod
    def _abrir(url, data=None, timeout=20):
        """HTTP com 3 tentativas (evita flake de socket quando a suíte está cheia)."""
        ultimo = None
        for _ in range(3):
            try:
                pedido = urllib.request.Request(
                    url, data=data, method="POST" if data is not None else "GET"
                )
                if data is not None:
                    pedido.add_header("Content-Type", "application/json")
                return urllib.request.urlopen(pedido, timeout=timeout).read()
            except (urllib.error.URLError, ConnectionError) as exc:
                ultimo = exc
                time.sleep(0.25)
        raise AssertionError(f"não consegui falar com o painel: {ultimo}")

    @staticmethod
    def esperar(app, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if app.job["status"] != "running":
                return app.job
            time.sleep(0.1)
        raise AssertionError("a rodada do painel não terminou")

    def test_preview_conta_catalogo_e_selecao(self):
        server = self.servidor()
        dados = server.app.preview("shopee", "low")
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["integration_id"], 10001)
        self.assertEqual(dados["mapped_total"], 12)
        self.assertEqual(dados["low_stock"], 6)
        self.assertEqual(dados["selected"], 6)
        self.assertEqual(self.fake.update_calls, 0, "preview não pode atualizar nada")

    def test_preview_modo_tudo(self):
        server = self.servidor()
        dados = server.app.preview("ml1", "all")
        self.assertEqual(dados["selected"], 8)

    def test_botao_simulacao_nao_envia(self):
        server = self.servidor()
        server.app.start("shopee", mode="low", dry_run=True, low_stock_max=10, speed="normal")
        job = self.esperar(server.app)
        self.assertEqual(job["status"], "done")
        self.assertIn("simulação", job["message"])
        self.assertEqual(self.fake.update_calls, 0)
        self.assertEqual(job["summary"]["selected"], 6)

    def test_botao_sincronizar_envia_so_o_canal_escolhido(self):
        server = self.servidor()
        server.app.start("ml1", mode="low", dry_run=False, low_stock_max=10, speed="normal")
        job = self.esperar(server.app)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["counters"]["ok"], 4)
        self.assertEqual(self.fake.update_calls, 4, "só o canal escolhido pode receber chamadas")

    def test_max_items_limita_a_fila(self):
        server = self.servidor()
        server.app.start("ml2", mode="all", dry_run=True, speed="normal", max_items=3)
        job = self.esperar(server.app)
        self.assertEqual(job["summary"]["selected"], 3)

    def test_duas_rodadas_ao_mesmo_tempo_sao_recusadas(self):
        server = self.servidor()
        self.fake.update_delay = 0.4
        server.app.start("shopee", mode="low", dry_run=False, speed="normal")
        with self.assertRaises(RuntimeError):
            server.app.start("ml1", mode="low", dry_run=False, speed="normal")
        self.esperar(server.app)

    def test_http_status_rodar_e_cancelar(self):
        server = self.servidor()
        base = server.url
        estado = json.loads(self._abrir(f"{base}/api/status"))
        self.assertEqual(len(estado["channels"]), 3)
        self.assertEqual(estado["job"]["status"], "idle")

        payload = json.dumps({"channel": "shopee", "mode": "low", "dryRun": True, "lowStockMax": 10}).encode()
        resposta = json.loads(self._abrir(f"{base}/api/run", data=payload, timeout=30))
        self.assertTrue(resposta["ok"])
        job = self.esperar(server.app)
        self.assertEqual(job["status"], "done")

        depois = json.loads(self._abrir(f"{base}/api/cancel", data=b"{}"))
        self.assertFalse(depois["ok"], "sem rodada em andamento, cancelar devolve ok=false")

    def test_pagina_tem_os_controles_e_fala_com_a_api(self):
        server = self.servidor()
        html = self._abrir(server.url).decode("utf-8")
        for marcador in ("maxitens", "só simular", "Estoque baixo", "/api/events", "/api/run"):
            self.assertIn(marcador, html)
        estado = json.loads(self._abrir(f"{server.url}/api/status"))
        nomes = [canal["name"] for canal in estado["channels"]]
        self.assertEqual(len(nomes), 3)
        self.assertTrue(any("Shopee" in nome for nome in nomes))
        self.assertTrue(any("Mercado Livre" in nome for nome in nomes))

    def test_painel_roda_os_tres_em_paralelo_e_expoe_estado_ao_vivo(self):
        server = self.servidor()
        payload = json.dumps(
            {"channels": ["shopee", "ml1", "ml2"], "parallel": True, "mode": "low", "dryRun": True}
        ).encode()
        resposta = json.loads(self._abrir(f"{server.url}/api/run", data=payload, timeout=30))
        self.assertTrue(resposta["ok"])
        self.assertEqual(sorted(resposta["job"]["channels"]), ["ml1", "ml2", "shopee"])
        self.assertTrue(resposta["job"]["parallel"])

        job = self.esperar(server.app, timeout=120)
        self.assertEqual(job["status"], "done")

        live = server.app.snapshot()["live"]
        self.assertIsNotNone(live, "o painel precisa enxergar o estado ao vivo")
        self.assertEqual(sorted(live["channels"]), ["ml1", "ml2", "shopee"])
        self.assertEqual(live["totals"]["selected"], 6 + 4 + 5)
        self.assertFalse(live["running"])
        self.assertIn("Sincronizar os 3", self._abrir(server.url).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()

