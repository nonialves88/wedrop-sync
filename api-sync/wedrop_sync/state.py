"""Estado em disco: logs, artefatos e o `last-run.json`.

Por que JSON estruturado além do log: a rotina antiga decidia se devia avisar
lendo o log anterior "no olho". Aqui a rodada grava um resumo com números
exatos, e a política de alerta (report.py) decide sobre dados — determinístico
e testável.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_HISTORY = 200


def _console_stream():
    """stdout em UTF-8 com `errors='replace'`.

    Console do Windows (cp1252) e locale de cron (C/POSIX) não aceitam acento
    nem emoji: sem isso o handler de log engasga e derruba a rodada justo na
    hora de avisar.
    """
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        return stream
    except (AttributeError, ValueError, OSError):
        pass
    try:
        return io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError, OSError):
        return stream


def configure_logging(log_path: Optional[Path], quiet: bool = False) -> logging.Logger:
    """Log em arquivo **e** stdout, com flush por linha (rodada pode morrer do nada)."""
    logger = logging.getLogger("wedrop")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    stream = logging.StreamHandler(stream=_console_stream())
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if quiet:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.WARNING)
    return logger


def rotate_log(log_path: Path, previous_path: Path) -> bool:
    """Copia o log da rodada anterior antes de começar a nova (como a rotina fazia)."""
    if not log_path.exists():
        return False
    try:
        shutil.copyfile(log_path, previous_path)
        return True
    except OSError:
        return False


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def append_history(path: Path, record: Dict[str, Any], limit: int = MAX_HISTORY) -> None:
    """Histórico curto em JSONL: mantém só as últimas `limit` rodadas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    try:
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        existing = []
    existing.append(line)
    if len(existing) > limit:
        existing = existing[-limit:]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_history(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return records


class ArtifactLog:
    """JSONL por canal/rodada: uma linha por anúncio, para auditoria depois."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")

    def write(self, record: Dict[str, Any]) -> None:
        try:
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass

    def __enter__(self) -> "ArtifactLog":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def done_ids_path(state_dir: Path, key: str) -> Path:
    return state_dir / f"done-{key}.json"


# --------------------------------------------------------------------------- #
# Estado ao vivo da rodada (para a barra de progresso funcionar mesmo quando
# a rodada foi disparada fora do painel: CLI, cron, outro terminal)
# --------------------------------------------------------------------------- #
def live_path(state_dir: Path) -> Path:
    return state_dir / "live.json"


def write_live(state_dir: Path, payload: Dict[str, Any]) -> None:
    try:
        write_json_atomic(live_path(state_dir), payload)
    except OSError:
        pass  # telemetria de UI nunca pode derrubar a rodada


def read_live(state_dir: Path) -> Optional[Dict[str, Any]]:
    data = read_json(live_path(state_dir), default=None)
    return data if isinstance(data, dict) else None


def clear_live(state_dir: Path) -> None:
    try:
        live_path(state_dir).unlink(missing_ok=True)
    except OSError:
        pass


def load_done_ids(path: Path) -> set:
    data = read_json(path, default=None)
    if isinstance(data, dict):
        data = data.get("ids") or []
    if not isinstance(data, list):
        return set()
    return {str(value) for value in data}


def save_done_ids(path: Path, values: Iterable[Any], cap: int = 50000) -> None:
    items = sorted({str(v) for v in values})
    if len(items) > cap:
        items = items[-cap:]
    write_json_atomic(path, {"ids": items})
