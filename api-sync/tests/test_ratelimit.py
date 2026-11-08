"""Testes do gate de rate limit e da serialização das atualizações."""

from __future__ import annotations

import unittest

from wedrop_sync.client import Response
from wedrop_sync.ratelimit import MARKETPLACE_CANDIDATES, RateLimitProbe, SerializedPoster


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


class FakeApi:
    """Stub do ApiClient: devolve o gate conforme o marketplace do path."""

    def __init__(self, respostas=None, corpo=None):
        self.respostas = respostas or {}
        self.corpo = corpo or {}
        self.caminhos = []

    def get(self, path, params=None, attempts=None):
        self.caminhos.append(path)
        for marketplace in MARKETPLACE_CANDIDATES:
            if f"/rate-limit/{marketplace}/" in path:
                status = self.respostas.get(marketplace, 404)
                if status == 200:
                    return Response(200, {"success": True, "data": self.corpo})
                return Response(status, {"error": "x"})
        return Response(404, {})


class RateLimitProbeTests(unittest.TestCase):
    def test_le_o_gate_e_aceita_o_mesmo_campo_de_mercado(self):
        api = FakeApi({"mercadolivre": 200}, {"integrationId": 10002, "canMakeRequests": True, "requestCount": 0})
        probe = RateLimitProbe(api, 10002, ttl=0, clock=FakeClock().monotonic, sleep=lambda s: None)
        estado = probe.refresh()
        self.assertEqual(estado["canMakeRequests"], True)
        self.assertEqual(probe.consultas, 1)
        self.assertIn("/internal/rate-limit/mercadolivre/10002", api.caminhos[0])

    def test_quando_o_gate_esta_fechado_espera_o_backoff(self):
        clock = FakeClock()
        corpo = {
            "canMakeRequests": False,
            "isInBackoff": True,
            "consecutive429s": 3,
            "backoffUntil": (10**13),  # bem no futuro (ms)
        }
        api = FakeApi({"mercadolivre": 200}, corpo)
        probe = RateLimitProbe(api, 10003, ttl=0, max_wait=30, clock=clock.monotonic, sleep=clock.sleep)
        probe.wait_if_needed()
        self.assertEqual(probe.waits, 1)
        self.assertEqual(clock.sleeps, [30.0], "respeita o teto de espera")

    def test_gate_aberto_nao_espera(self):
        clock = FakeClock()
        api = FakeApi({"mercadolivre": 200}, {"canMakeRequests": True})
        probe = RateLimitProbe(api, 10001, ttl=10, clock=clock.monotonic, sleep=clock.sleep)
        probe.wait_if_needed()
        probe.wait_if_needed()
        self.assertEqual(clock.sleeps, [], "sem espera quando pode enviar")
        self.assertEqual(probe.consultas, 1, "usa cache dentro do TTL")

    def test_endpoint_inexistente_desliga_a_sonda(self):
        clock = FakeClock()
        api = FakeApi({})  # tudo 404
        probe = RateLimitProbe(api, 10001, ttl=0, clock=clock.monotonic, sleep=clock.sleep)
        self.assertIsNone(probe.refresh())
        self.assertIsNone(probe.wait_if_needed())
        consultas = probe.consultas
        self.assertIsNone(probe.refresh())
        self.assertEqual(probe.consultas, consultas, "não fica batendo num endpoint que não existe")

    def test_note_429_invalida_o_cache(self):
        clock = FakeClock()
        api = FakeApi({"mercadolivre": 200}, {"canMakeRequests": True})
        probe = RateLimitProbe(api, 10002, ttl=60, clock=clock.monotonic, sleep=clock.sleep)
        probe.refresh()
        probe.note_429()
        probe.refresh()
        self.assertEqual(probe.consultas, 2)


class SerializedPosterTests(unittest.TestCase):
    def test_serializa_com_intervalo(self):
        clock = FakeClock()
        poster = SerializedPoster(2.0, clock=clock.monotonic, sleep=clock.sleep)
        for _ in range(4):
            poster.wait_turn()
        # 1ª chamada imediata, depois 3 esperas de 2s
        self.assertEqual(clock.sleeps, [2.0, 2.0, 2.0])
        self.assertEqual(poster.sent, 4)
        self.assertEqual(poster.effective_workers, 1)

    def test_strict_false_nao_espera(self):
        clock = FakeClock()
        poster = SerializedPoster(2.0, strict=False, clock=clock.monotonic, sleep=clock.sleep)
        for _ in range(3):
            poster.wait_turn()
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
