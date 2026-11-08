"""Controle de ritmo: pausa entre envios e concorrência adaptativa.

Motivo: as rodadas reais passam de 9 horas num canal. Dois botões que mudam
isso sem trocar a lógica do fluxo:

* `Pacer`  — espaçamento mínimo entre requisições (o `--delay`), com jitter
  para não sincronizar os workers;
* `Gate`   — quantas requisições podem estar em voo ao mesmo tempo. Se a API
  responder 429, a porta se fecha (menos concorrência) e reabre aos poucos
  depois de um período de sucesso. Isso evita a escolha errada entre "vai
  devagar para sempre" e "leva 429 em cascata".
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional


class Pacer:
    """Espaça as requisições: no mínimo `delay` segundos entre envios."""

    def __init__(
        self,
        delay: float,
        jitter: float = 0.05,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.delay = max(0.0, float(delay))
        self.jitter = max(0.0, float(jitter))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.sent = 0

    def wait(self) -> None:
        if self.delay <= 0:
            with self._lock:
                self.sent += 1
            return
        with self._lock:
            now = self._clock()
            target = self._next_at if self._next_at > now else now
            step = self.delay + (random.uniform(0, self.jitter) if self.jitter else 0.0)
            self._next_at = target + step
            self.sent += 1
            sleep_for = target - now
        if sleep_for > 0:
            self._sleep(sleep_for)


class Gate:
    """Limita requisições em voo e se adapta a 429 / 5xx."""

    def __init__(
        self,
        limit: int,
        minimum: int = 1,
        *,
        cooldown: float = 30.0,
        grow_after: int = 25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_limit = max(1, int(limit))
        self.minimum = max(1, min(int(minimum), self.max_limit))
        self._limit = self.max_limit
        self.cooldown = cooldown
        self.grow_after = max(1, int(grow_after))
        self._clock = clock
        self._sleep = sleep
        self._cv = threading.Condition()
        self._inflight = 0
        self._cool_until = 0.0
        self._success_streak = 0
        self.shrinks = 0
        self.grows = 0

    # ---------------------------------------------------------------- #
    @property
    def limit(self) -> int:
        with self._cv:
            return self._limit

    def acquire(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else self._clock() + timeout
        with self._cv:
            while self._inflight >= self._limit:
                remaining = None if deadline is None else deadline - self._clock()
                if remaining is not None and remaining <= 0:
                    return False
                self._cv.wait(timeout=remaining)
            self._inflight += 1
            return True

    def release(self) -> None:
        with self._cv:
            if self._inflight > 0:
                self._inflight -= 1
            self._cv.notify_all()

    # ---------------------------------------------------------------- #
    def on_rate_limited(self, retry_after: float = 0.0) -> None:
        """429: fecha a porta e trava reabertura por um tempo."""
        with self._cv:
            if self._limit > self.minimum:
                self._limit -= 1
                self.shrinks += 1
            self._success_streak = 0
            self._cool_until = self._clock() + max(self.cooldown, float(retry_after))

    def on_server_error(self) -> None:
        """5xx: mesma coisa, sem esperar tanto."""
        with self._cv:
            if self._limit > self.minimum:
                self._limit -= 1
                self.shrinks += 1
            self._success_streak = 0
            self._cool_until = max(self._cool_until, self._clock() + min(self.cooldown, 10.0))

    def on_success(self) -> None:
        """Reabre aos poucos, depois do cooldown."""
        with self._cv:
            if self._limit >= self.max_limit:
                return
            self._success_streak += 1
            if self._success_streak < self.grow_after:
                return
            if self._clock() < self._cool_until:
                return
            self._limit += 1
            self._success_streak = 0
            self.grows += 1
            self._cv.notify_all()

    def __enter__(self) -> "Gate":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def stats(self) -> dict:
        with self._cv:
            return {"limit": self._limit, "max": self.max_limit, "shrinks": self.shrinks, "grows": self.grows}
