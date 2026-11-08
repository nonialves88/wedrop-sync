"""Testes de unidade: config, classificação, autenticação, fila, ritmo e trava."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.mockapi import make_token

from wedrop_sync import classify, config, state
from wedrop_sync.auth import AuthError, TokenProvider, jwt_expiry
from wedrop_sync.client import Response
from wedrop_sync.items import build_queue, collect, extract_items, extract_total, to_item
from wedrop_sync.lock import LockBusy, RunLock, read_owner
from wedrop_sync.throttle import Gate, Pacer


class TempDirMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="wedrop-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)


# --------------------------------------------------------------------------- #
# Configuração e credenciais
# --------------------------------------------------------------------------- #
class EnvFileTests(TempDirMixin):
    def write(self, text: str) -> Path:
        path = self.dir / "creds.env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_le_formato_tolerante(self):
        path = self.write(
            "# comentário\n\n"
            'export WEDROP_EMAIL = "marco@loja.com"\n'
            "USUARIO=outro@x\n"
            "SENHA = s3nh4 # comentário no fim\n"
            "WEDROP_PASSWORD='com espaco'\n"
            "linha sem igual\n"
        )
        values = config.read_env_file(path)
        self.assertEqual(values["wedrop_email"], "marco@loja.com")
        self.assertEqual(values["usuario"], "outro@x")
        self.assertEqual(values["senha"], "s3nh4")
        self.assertEqual(values["wedrop_password"], "com espaco")

    def test_arquivo_ausente_devolve_vazio(self):
        self.assertEqual(config.read_env_file(self.dir / "nada.env"), {})

    def test_credenciais_com_chaves_usuario_senha(self):
        path = self.write("usuario = marco@loja.com\nsenha = segredo\n")
        creds = config.load_credentials(path, env={})
        self.assertEqual(creds.email, "marco@loja.com")
        self.assertEqual(creds.password, "segredo")

    def test_ambiente_tem_precedencia_sobre_arquivo(self):
        path = self.write("WEDROP_EMAIL=arquivo@x\nWEDROP_PASSWORD=arquivo\n")
        creds = config.load_credentials(path, env={"WEDROP_EMAIL": "env@x", "WEDROP_PASSWORD": "env"})
        self.assertEqual(creds.email, "env@x")
        self.assertEqual(creds.password, "env")

    def test_falta_de_credencial_explica_sem_vazar_senha(self):
        path = self.write("WEDROP_EMAIL=so-email@x\n")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_credentials(path, env={})
        self.assertIn("WEDROP_PASSWORD", str(ctx.exception))
        self.assertNotIn("so-email", str(ctx.exception))

    def test_senha_nunca_aparece_no_repr(self):
        creds = config.Credentials("marco@loja.com", "super-secreta", "teste")
        self.assertNotIn("super-secreta", repr(creds))
        self.assertNotIn("super-secreta", str(creds))
        self.assertIn("ma***@loja.com", repr(creds))


class OrderTests(TempDirMixin):
    def test_ordem_preservada_e_sem_duplicata(self):
        order = config.parse_order("shopee, ml1 , ml1, ml2")
        self.assertEqual([c.key for c in order], ["shopee", "ml1", "ml2"])

    def test_ordem_customizada(self):
        order = config.parse_order("ml2,shopee")
        self.assertEqual([c.key for c in order], ["ml2", "shopee"])

    def test_bling_e_ignorado(self):
        self.assertEqual([c.key for c in config.parse_order("shopee,bling")], ["shopee"])

    def test_canal_desconhecido_erro(self):
        with self.assertRaises(config.ConfigError):
            config.parse_order("shopee,xpto")

    def test_lista_vazia_erro(self):
        with self.assertRaises(config.ConfigError):
            config.parse_order(" , ")


class SettingsTests(TempDirMixin):
    def test_precedencia_cli_env_padrao(self):
        env = {"WEDROP_HOME": str(self.dir), "WEDROP_WORKERS": "9", "WEDROP_DELAY": "0.9"}
        self.assertEqual(config.load_settings({"workers": 7, "delay": 0.5}, env=env).workers, 7)
        self.assertEqual(config.load_settings({}, env=env).workers, 9)
        self.assertEqual(config.load_settings({}, env={"WEDROP_HOME": str(self.dir)}).workers, 3)

    def test_modo_completo_por_ambiente(self):
        settings = config.load_settings({}, env={"WEDROP_HOME": str(self.dir), "WEDROP_ALL": "1"})
        self.assertTrue(settings.all_items)

    def test_workers_invalido(self):
        with self.assertRaises(config.ConfigError):
            config.load_settings({"workers": 0}, env={"WEDROP_HOME": str(self.dir)})

    def test_caminhos_derivados(self):
        settings = config.load_settings({"home": str(self.dir)}, env={})
        self.assertEqual(settings.lock_path, self.dir / ".sync.lock")
        self.assertEqual(settings.last_run_json, self.dir / "state" / "last-run.json")
        self.assertEqual(settings.prev_log_path, self.dir / "prev-run.log")

    def test_credencial_padrao_fora_do_projeto(self):
        """Sem --env-file, cai em ~/.wedrop/creds.env (fora do projeto/OneDrive)."""
        home_falso = self.dir / "home-padrao"
        (home_falso).mkdir(parents=True, exist_ok=True)
        (home_falso / "creds.env").write_text("WEDROP_EMAIL=a@b.c\nWEDROP_PASSWORD=x\n", encoding="utf-8")

        with mock.patch.object(config, "DEFAULT_HOME", home_falso):
            settings = config.load_settings({"home": str(self.dir / "projeto")}, env={})
        self.assertEqual(settings.creds_file, home_falso / "creds.env")

    def test_env_file_explicito_tem_precedencia(self):
        home_falso = self.dir / "home-padrao"
        home_falso.mkdir(parents=True, exist_ok=True)
        (home_falso / "creds.env").write_text("WEDROP_EMAIL=a@b.c\nWEDROP_PASSWORD=x\n", encoding="utf-8")
        escolhido = self.dir / "outro.env"
        escolhido.write_text("WEDROP_EMAIL=c@d.e\nWEDROP_PASSWORD=y\n", encoding="utf-8")

        with mock.patch.object(config, "DEFAULT_HOME", home_falso):
            settings = config.load_settings({"home": str(self.dir), "env_file": str(escolhido)}, env={})
        self.assertEqual(settings.creds_file, escolhido)


# --------------------------------------------------------------------------- #
# Classificação
# --------------------------------------------------------------------------- #
class ClassifyTests(unittest.TestCase):
    def test_sucesso(self):
        self.assertEqual(classify.classify_update(200, {"success": True}).outcome, classify.Outcome.OK)

    def test_parcial_e_pulado(self):
        self.assertEqual(classify.classify_update(200, {"partialSuccess": True}).outcome, classify.Outcome.PARTIAL)
        skipped = classify.classify_update(200, {"skipped": True})
        self.assertEqual(skipped.outcome, classify.Outcome.SKIPPED)
        self.assertTrue(skipped.benign)

    def test_orfao_benigno(self):
        verdict = classify.classify_update(
            200, {"success": False, "errors": [{"message": "O canal não encontrou este anúncio"}]}
        )
        self.assertEqual(verdict.outcome, classify.Outcome.SKIPPED)
        self.assertEqual(verdict.reason, classify.R_ORPHAN)
        self.assertTrue(verdict.benign)

    def test_anuncio_em_revisao(self):
        verdict = classify.classify_message("anúncio UNDER_REVIEW")
        self.assertEqual(verdict.reason, classify.R_CHANNEL_STATUS)
        self.assertTrue(verdict.benign)

    def test_perda_de_autorizacao(self):
        verdict = classify.classify_message("A conexão com esta loja perdeu a autorização")
        self.assertEqual(verdict.reason, classify.R_AUTH_LOSS)
        self.assertFalse(verdict.benign)

    def test_variavel_sem_model_id(self):
        verdict = classify.classify_message("model_id is mandatory for variations")
        self.assertEqual(verdict.reason, classify.R_MISSING_MODEL)

    def test_codigos_http(self):
        self.assertEqual(classify.classify_update(429, {}).reason, classify.R_RATE_LIMIT)
        self.assertTrue(classify.classify_update(429, {}).retryable)
        self.assertEqual(classify.classify_update(401, {}).reason, classify.R_AUTH_LOSS)
        self.assertEqual(classify.classify_update(503, {}).reason, classify.R_HTTP_5XX)

    def test_success_false_sem_erro(self):
        self.assertEqual(classify.classify_update(200, {"success": False}).reason, classify.R_UNKNOWN)

    def test_erros_como_string(self):
        verdict = classify.classify_update(200, {"success": False, "errors": "falha qualquer"})
        self.assertEqual(verdict.outcome, classify.Outcome.ERROR)
        self.assertEqual(verdict.reason, classify.R_ITEM_ERROR)

    def test_acentos_e_caixa(self):
        self.assertEqual(classify.classify_message("ANÚNCIO NÃO ENCONTRADO").reason, classify.R_ORPHAN)

    def test_leitura(self):
        self.assertTrue(classify.classify_read(200, {}).ok)
        self.assertTrue(classify.classify_read(502, {}).retryable)

    def test_corpo_real_de_perda_de_autorizacao(self):
        """Payload capturado ao vivo no ML1 (403 do Mercado Livre)."""
        corpo = {
            "results": [],
            "errors": [
                {
                    "itemProductId": 3374833,
                    "itemProductKey": "MLB4548499747",
                    "error": "PUT /items/MLB4548499747 falhou: mercadolivre API Error: "
                    "The caller is not authorized to access this resource | forbidden | HTTP 403",
                    "errorType": "PUT_ITEMS_FAILED",
                    "mensagem": "A conexão com esta loja perdeu a autorização. "
                    "Reconecte a integração para voltar a atualizar o estoque.",
                }
            ],
            "success": False,
            "partialSuccess": False,
            "message": "1 erro(s)",
            "mensagem": "A conexão com esta loja perdeu a autorização. "
            "Reconecte a integração para voltar a atualizar o estoque.",
        }
        verdict = classify.classify_update(200, corpo)
        self.assertEqual(verdict.outcome, classify.Outcome.ERROR)
        self.assertEqual(verdict.reason, classify.R_AUTH_LOSS, "precisa virar perda de autorização, não erro genérico")
        self.assertIn("perdeu a autorização", verdict.message)
        self.assertFalse(verdict.benign)

    def test_corpo_real_de_sucesso_por_produto(self):
        corpo = {
            "results": [
                {
                    "itemProductId": 6741916,
                    "itemProductKey": "MLB5406129907",
                    "availableQuantity": 0,
                    "success": True,
                    "mlRaw": {"primary": {"endpoint": "PUT /items/MLB5406129907", "status": 200}},
                }
            ]
        }
        verdict = classify.classify_update(200, corpo)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.sent_quantity, 0)


# --------------------------------------------------------------------------- #
# Autenticação
# --------------------------------------------------------------------------- #
class StubTransport:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json_body")))
        return self.responder(method, path, kwargs)

    def close(self):
        pass


class AuthTests(unittest.TestCase):
    def credentials(self):
        return config.Credentials("marco@loja.com", "senha", "teste")

    def test_le_exp_do_jwt(self):
        exp = jwt_expiry(make_token(ttl=600))
        self.assertIsNotNone(exp)
        self.assertLess(abs(exp - (time.time() + 600)), 10)

    def test_token_invalido_sem_exp(self):
        self.assertIsNone(jwt_expiry("nao-e-jwt"))
        self.assertIsNone(jwt_expiry(""))

    def test_login_uma_vez_e_cache(self):
        transport = StubTransport(lambda m, p, k: Response(200, {"access_token": make_token(3600)}))
        provider = TokenProvider(transport, self.credentials())
        first, second = provider.token(), provider.token()
        self.assertEqual(first, second)
        self.assertEqual(provider.logins, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_renova_antes_de_expirar(self):
        transport = StubTransport(lambda m, p, k: Response(200, {"access_token": make_token(60)}))
        provider = TokenProvider(transport, self.credentials(), margin=600)
        provider.token()
        provider.token()
        self.assertEqual(provider.logins, 2)

    def test_invalidate_forca_novo_login(self):
        transport = StubTransport(lambda m, p, k: Response(200, {"access_token": make_token(3600)}))
        provider = TokenProvider(transport, self.credentials())
        provider.token()
        provider.invalidate()
        provider.token()
        self.assertEqual(provider.logins, 2)

    def test_codigo_por_email(self):
        transport = StubTransport(
            lambda m, p, k: Response(200, {"requiresCode": True, "message": "enviamos um código de verificação"})
        )
        with self.assertRaises(AuthError) as ctx:
            TokenProvider(transport, self.credentials()).token()
        self.assertEqual(ctx.exception.kind, "email_code")
        self.assertTrue(ctx.exception.needs_human)

    def test_credencial_invalida(self):
        transport = StubTransport(lambda m, p, k: Response(401, {"message": "usuário ou senha inválidos"}))
        with self.assertRaises(AuthError) as ctx:
            TokenProvider(transport, self.credentials()).token()
        self.assertEqual(ctx.exception.kind, "invalid_credentials")
        self.assertTrue(ctx.exception.needs_human)

    def test_falha_de_rede(self):
        def explode(method, path, kwargs):
            raise OSError("sem rede")

        with self.assertRaises(AuthError) as ctx:
            TokenProvider(StubTransport(explode), self.credentials()).token()
        self.assertEqual(ctx.exception.kind, "network")
        self.assertFalse(ctx.exception.needs_human)

    def test_envio_da_senha_no_corpo(self):
        transport = StubTransport(lambda m, p, k: Response(200, {"access_token": make_token(3600)}))
        TokenProvider(transport, self.credentials()).token()
        _, path, body = transport.calls[0]
        self.assertEqual(path, "/auth")
        self.assertEqual(body["email"], "marco@loja.com")
        self.assertEqual(body["password"], "senha")


# --------------------------------------------------------------------------- #
# Fila
# --------------------------------------------------------------------------- #
CATALOG = [
    {"id": "a-1", "stock": 3, "outdatedStock": False},
    {"id": "a-2", "stock": 50, "outdatedStock": True},
    {"id": "a-3", "stock": 5, "outdatedStock": True},
    {"id": "a-4", "stock": 70, "outdatedStock": False},
    {"id": "a-5", "stock": 2, "outdatedStock": False},
]


class ListApi:
    """Stub que pagina e filtra como a API real."""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def get(self, path, params=None, attempts=None):
        params = dict(params or {})
        self.calls.append(params)
        items = [dict(entry) for entry in self.items]
        if params.get("lowStock") in ("true", True):
            items = [entry for entry in items if entry.get("stock", 0) <= 10]
        if params.get("outdatedStock") in ("true", True):
            items = [entry for entry in items if entry.get("outdatedStock")]
        limit = int(params.get("limit") or 50)
        offset = int(params.get("offset") or 0)
        return Response(200, {"items": items[offset : offset + limit], "total": len(items)})


class ItemTests(unittest.TestCase):
    def test_extrai_lista_em_varios_formatos(self):
        self.assertEqual(len(extract_items({"items": [{"id": 1}]})), 1)
        self.assertEqual(len(extract_items({"data": {"items": [{"id": 1}, {"id": 2}]}})), 2)
        self.assertEqual(len(extract_items([{"id": 1}])), 1)
        self.assertEqual(extract_items({"nada": 1}), [])

    def test_extrai_total(self):
        self.assertEqual(extract_total({"total": "42"}), 42)
        self.assertEqual(extract_total({"meta": {"total": 7}}), 7)
        self.assertIsNone(extract_total({}))

    def test_item_com_chaves_alternativas(self):
        item = to_item({"itemId": 99, "name": "Capa", "sellerSku": "S1", "stock_outdated": "true"})
        self.assertEqual(str(item.item_id), "99")
        self.assertEqual(item.title, "Capa")
        self.assertEqual(item.sku, "S1")
        self.assertTrue(item.outdated)

    def test_item_sem_id_e_ignorado(self):
        self.assertIsNone(to_item({"title": "sem id"}))

    def test_collect_pagina(self):
        api = ListApi(CATALOG)
        items, total, truncated = collect(api, integration_id=1, user_id=1, low_stock=False, page_size=2)
        self.assertEqual(len(items), 5)
        self.assertEqual(total, 5)
        self.assertFalse(truncated)
        self.assertEqual(len(api.calls), 3)  # 2 + 2 + 1

    def test_collect_respeita_max(self):
        api = ListApi(CATALOG)
        items, _, truncated = collect(api, integration_id=1, user_id=1, low_stock=False, page_size=2, max_items=3)
        self.assertEqual(len(items), 3)
        self.assertTrue(truncated)

    def test_fila_prioriza_desatualizados(self):
        queue = build_queue(ListApi(CATALOG), integration_id=1, user_id=1, all_items=False)
        keys = [item.key for item in queue.items]
        # estoque baixo: a-1, a-3, a-5 · desatualizados entre eles: a-3
        self.assertEqual(keys, ["a-3", "a-1", "a-5"])
        self.assertTrue(queue.items[0].outdated)
        self.assertEqual(queue.duplicates, 1, "a-3 aparece nas duas consultas e é deduplicado")
        self.assertEqual(queue.mapped_total, 5)

    def test_fila_remove_duplicados_mesmo_sem_filtro_no_servidor(self):
        class SemFiltro:
            def get(self, path, params=None, attempts=None):
                return Response(200, {"items": [dict(entry) for entry in CATALOG], "total": len(CATALOG)})

        queue = build_queue(SemFiltro(), integration_id=1, user_id=1, all_items=True)
        self.assertEqual(len(queue.items), len(CATALOG))
        self.assertGreater(queue.duplicates, 0, "o item que aparece nas duas consultas precisa ser deduplicado")

    def test_fila_modo_completo(self):
        queue = build_queue(ListApi(CATALOG), integration_id=1, user_id=1, all_items=True)
        self.assertEqual(len(queue.items), 5)
        primeiros = [item.key for item in queue.items][:2]
        self.assertEqual(set(primeiros), {"a-2", "a-3"}, "os dois desatualizados vêm na frente")
        self.assertTrue(all(item.outdated for item in queue.items[:2]))


# --------------------------------------------------------------------------- #
# Ritmo
# --------------------------------------------------------------------------- #
class FakeClock:
    """Relógio controlado: deixa o teste do Pacer determinístico (sem depender do timer do Windows)."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ThrottleTests(unittest.TestCase):
    def test_pacer_espaca(self):
        clock = FakeClock()
        pacer = Pacer(0.02, jitter=0, clock=clock.monotonic, sleep=clock.sleep)
        for _ in range(4):
            pacer.wait()
        self.assertEqual(pacer.sent, 4)
        self.assertEqual(len(clock.sleeps), 3, "a 1ª chamada é imediata, as outras esperam o intervalo")
        self.assertAlmostEqual(sum(clock.sleeps), 0.06, places=6)

    def test_pacer_sem_delay_nao_espera(self):
        clock = FakeClock()
        pacer = Pacer(0.0, jitter=0, clock=clock.monotonic, sleep=clock.sleep)
        for _ in range(3):
            pacer.wait()
        self.assertEqual(clock.sleeps, [])

    def test_gate_limita_concorrencia(self):
        gate = Gate(2, minimum=1)
        self.assertTrue(gate.acquire())
        self.assertTrue(gate.acquire())
        self.assertFalse(gate.acquire(timeout=0.05))
        gate.release()
        self.assertTrue(gate.acquire(timeout=0.5))
        gate.release()
        gate.release()

    def test_gate_reduz_com_429_e_volta_depois(self):
        gate = Gate(3, minimum=1, cooldown=0.0, grow_after=2, clock=lambda: 1e9)
        gate.on_rate_limited()
        self.assertEqual(gate.limit, 2)
        gate.on_success()
        self.assertEqual(gate.limit, 2)
        gate.on_success()
        self.assertEqual(gate.limit, 3)
        self.assertEqual(gate.stats()["shrinks"], 1)
        self.assertEqual(gate.stats()["grows"], 1)


# --------------------------------------------------------------------------- #
# Trava
# --------------------------------------------------------------------------- #
class LockTests(TempDirMixin):
    def test_segunda_trava_falha(self):
        path = self.dir / ".sync.lock"
        first, second = RunLock(path), RunLock(path)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
            with self.assertRaises(LockBusy):
                second.acquire_or_raise()
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_pid_na_trava(self):
        path = self.dir / ".sync.lock"
        lock = RunLock(path)
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(read_owner(path), str(os.getpid()))
        finally:
            lock.release()

    def test_release_libera(self):
        path = self.dir / ".sync.lock"
        lock = RunLock(path)
        lock.acquire()
        lock.release()
        self.assertFalse(lock.held)
        self.assertTrue(RunLock(path).acquire())


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #
class LoggingTests(TempDirMixin):
    def test_log_com_emoji_e_acento_nao_quebra(self):
        """Console cp1252 (Windows) e locale C (cron) não podem derrubar a rodada."""
        logger = state.configure_logging(None)
        console = next(
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        )
        encoding = str(getattr(console.stream, "encoding", "")).lower()
        self.assertIn("utf", encoding)

        with mock.patch.object(logging.Handler, "handleError") as handle_error:
            logger.warning("ALERTA ⚠️ sincronização de estoque — atenção: perdeu a autorização")
        handle_error.assert_not_called()

    def test_log_vai_para_arquivo_em_utf8(self):
        path = self.dir / "last-run.log"
        logger = state.configure_logging(path)
        logger.info("verificação: %d de %d relidos", 0, 25)
        conteudo = path.read_text(encoding="utf-8")
        self.assertIn("verificação", conteudo)
        self.assertIn("0 de 25", conteudo)


if __name__ == "__main__":
    unittest.main()
