"""Sonda do rate limit interno da WeDrop + serialização das atualizações.

Duas regras vêm da engenharia reversa (documento de handoff do Claude, seção
3.3/3.4) e do que foi confirmado ao vivo:

1. **Nunca paralelizar `update-stock` no mesmo marketplace.** O limite é do
   marketplace (Shopee/ML), não da WeDrop: no lote real de 894 itens a cadência
   ficou em 1 item a cada 30-50s. Paralelizar é o caminho mais curto para
   bloqueio/captcha.
2. **Existe um gate no próprio WeDrop**: `GET /internal/rate-limit/{marketplace}/{integrationId}`
   devolve `canMakeRequests`, `backoffUntil`, `consecutive429s`, `requestCount`,
   `timeRemaining`. Melhor perguntar a ele do que adivinhar com sleep fixo.

Este módulo entrega as duas coisas: `RateLimitProbe` (consulta barata, com
cache) e `SerializedPoster` (transforma N workers em fila de 1 por
marketplace).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from .client import ApiClient, RateLimited, TransportError

#: primeiro segmento que funcionou na prática; guardamos o que respondeu 200.
MARKETPLACE_CANDIDATES = ("mercadolivre", "shopee", "mercado_livre", "ml")


class RateLimitProbe:
    """Consulta o gate do WeDrop e espera quando ele manda esperar.

    Best-effort por desenho: se o endpoint não existe para a integração, a
    sonda fica muda e a rodada segue no ritmo normal (com backoff em 429).
    """

    def __init__(
        self,
        api: ApiClient,
        integration_id: int,
        *,
        ttl: float = 10.0,
        max_wait: float = 300.0,
        logger: Optional[logging.Logger] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.integration_id = integration_id
        self.ttl = max(1.0, ttl)
        self.max_wait = max_wait
        self.log = logger or logging.getLogger("wedrop")
        self._sleep = sleep
        self._clock = clock
        self._marketplace: Optional[str] = None
        self._last_check = 0.0
        self._state: Optional[Dict[str, Any]] = None
        self._enabled = True
        self._lock = threading.Lock()
        self.waits = 0
        self.consultas = 0

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> Optional[Dict[str, Any]]:
        return self._state

    def _consultar(self) -> Optional[Dict[str, Any]]:
        candidatos = (self._marketplace,) if self._marketplace else MARKETPLACE_CANDIDATES
        for marketplace in candidatos:
            if not marketplace:
                continue
            try:
                resposta = self.api.get(
                    f"/internal/rate-limit/{marketplace}/{self.integration_id}", attempts=1
                )
            except (TransportError, RateLimited):
                return None
            if resposta.status == 200 and isinstance(resposta.data, dict):
                self._marketplace = marketplace
                corpo = resposta.data.get("data") if isinstance(resposta.data.get("data"), dict) else resposta.data
                return corpo if isinstance(corpo, dict) else None
            if resposta.status == 404:
                continue
            # 401/403/5xx: não insistir
            return None
        # nenhum caminho respondeu: desliga a sonda para não gastar requisição
        self._enabled = False
        self.log.info(
            "sonda de rate limit indisponível para a integração %s; seguindo pelo backoff de 429",
            self.integration_id,
        )
        return None

    def refresh(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return None
        with self._lock:
            agora = self._clock()
            if not force and self._state is not None and (agora - self._last_check) < self.ttl:
                return self._state
            estado = self._consultar()
            self.consultas += 1
            self._last_check = agora
            self._state = estado
            return estado

    def wait_if_needed(self) -> Optional[Dict[str, Any]]:
        """Antes de cada envio: se o gate estiver fechado, espera o tempo dele."""
        estado = self.refresh()
        if not estado:
            return None

        pode = estado.get("canMakeRequests")
        if pode is None:
            pode = not estado.get("isInBackoff", False)
        if pode:
            return estado

        agora_ms = time.time() * 1000
        backoff_ate = estado.get("backoffUntil") or 0
        espera_ms = max(0.0, float(backoff_ate) - agora_ms)
        espera = min(self.max_wait, espera_ms / 1000.0) if espera_ms > 0 else min(self.max_wait, 5.0)
        self.waits += 1
        self.log.warning(
            "rate limit do canal fechado (429 seguidos=%s): aguardando %.1fs",
            estado.get("consecutive429s"),
            espera,
        )
        self._sleep(espera)
        self._last_check = 0.0  # reconsulta depois da espera
        return estado

    def note_429(self) -> None:
        """Um 429 aconteceu: invalida o cache para reconsultar no próximo item."""
        with self._lock:
            self._last_check = 0.0


class SerializedPoster:
    """Serializa as chamadas de `update-stock` (regra: 1 por marketplace).

    Com `workers > 1` os itens são buscados/selecionados em paralelo, mas o
    POST sai um por vez, com o intervalo configurado. Se o usuário quiser
    abrir mão disso (não recomendado), `strict=False`.
    """

    def __init__(
        self,
        delay: float,
        *,
        strict: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.delay = max(0.0, float(delay))
        self.strict = strict
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock
        self._next_at = 0.0
        self.sent = 0

    @property
    def effective_workers(self) -> int:
        return 1 if self.strict else 0  # 0 = sem opinião (usa o do runner)

    def wait_turn(self) -> None:
        if not self.strict:
            return
        with self._lock:
            agora = self._clock()
            alvo = self._next_at if self._next_at > agora else agora
            self._next_at = alvo + self.delay
            self.sent += 1
            espera = alvo - agora
        if espera > 0:
            self._sleep(espera)
