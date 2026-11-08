"""Trava exclusiva de rodada.

Motivo: nunca rodar duas sincronizações ao mesmo tempo. O painel usa uma fila
"bulkqueue" só no navegador (via socket) — no servidor ela não existe, então a
garantia tem que ser nossa.

Implementação: `fcntl.flock` no POSIX e `msvcrt.locking` no Windows (para poder
testar e rodar no notebook também). A trava é liberada pelo sistema operacional
quando o processo morre, então não existe trava "presa" de rodada anterior.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

WINDOWS = os.name == "nt"


class LockBusy(RuntimeError):
    """Outra rodada já está com a trava."""


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    # ------------------------------------------------------------------ #
    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, timeout: float = 0.0, poll: float = 0.25) -> bool:
        """Tenta pegar a trava. `timeout=0` = não espera (comportamento da rotina)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        waited = 0.0
        while True:
            fh = self._open()
            if self._lock(fh):
                self._fh = fh
                self._write_owner(fh)
                return True
            fh.close()
            if waited >= timeout:
                return False
            import time

            time.sleep(poll)
            waited += poll

    def acquire_or_raise(self, timeout: float = 0.0) -> None:
        if not self.acquire(timeout=timeout):
            owner = read_owner(self.path)
            detail = f" (pid {owner})" if owner else ""
            raise LockBusy(f"já existe uma sincronização em andamento{detail}")

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            self._unlock(fh)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self) -> "RunLock":
        self.acquire_or_raise()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    # ------------------------------------------------------------------ #
    def _open(self):
        # "a+" cria se não existir e não trunca o conteúdo (útil para ler o pid).
        return open(self.path, "a+", encoding="utf-8")

    def _write_owner(self, fh) -> None:
        # No Windows a região travada não é legível por outro processo, então o
        # pid também vai para um arquivo vizinho (usado só em mensagens).
        try:
            sidecar = self.path.with_suffix(self.path.suffix + ".pid")
            sidecar.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:
            pass
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            pass

    @staticmethod
    def _lock(fh) -> bool:
        if WINDOWS:
            import msvcrt

            try:
                fh.seek(0)
                if fh.read(1) == "":
                    fh.seek(0)
                    fh.write(" ")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    @staticmethod
    def _unlock(fh) -> None:
        if WINDOWS:
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_owner(path: Path) -> Optional[str]:
    """Lê o pid gravado na trava (diagnóstico de "quem está rodando")."""
    locker = Path(path)
    candidates = [locker.with_suffix(locker.suffix + ".pid"), locker]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        first = text.splitlines()[0].strip() if text else ""
        if first.isdigit():
            return first
    return None
