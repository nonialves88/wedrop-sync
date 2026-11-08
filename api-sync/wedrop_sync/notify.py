"""Envio dos avisos.

Sinks opcionais, escolhidos por configuração/env:
  * `--webhook URL`        → POST JSON {"text": ...} (Discord/Slack/Zapier/n8n);
  * `--notify-command CMD` → executa um comando e passa a mensagem no stdin
                             (útil para: telegram-send, curl, script próprio);
  * `--notify-file PATH`   → acrescenta a mensagem num arquivo;
  * sempre: uma linha no log (`ALERTA ...`).

Se nada estiver configurado, o aviso só vai para o stdout/log — a rotina
agendada lê isso (ou usa `--webhook` para chegar no celular).
"""
from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from .config import Settings


class Sink:
    name = "sink"

    def send(self, text: str) -> str:
        raise NotImplementedError


class WebhookSink(Sink):
    name = "webhook"

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, text: str) -> str:
        payload = json.dumps({"text": text, "content": text, "source": "wedrop-sync"}).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return f"webhook ok ({response.status})"
        except urllib.error.HTTPError as exc:
            return f"webhook falhou (HTTP {exc.code})"
        except (urllib.error.URLError, OSError) as exc:
            return f"webhook falhou ({type(exc).__name__})"


class CommandSink(Sink):
    name = "comando"

    def __init__(self, command: str, timeout: float = 60.0) -> None:
        self.command = command
        self.timeout = timeout

    def send(self, text: str) -> str:
        try:
            completed = subprocess.run(
                self.command,
                shell=True,
                input=text,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"comando falhou ({type(exc).__name__})"
        if completed.returncode != 0:
            return f"comando falhou (exit {completed.returncode})"
        return "comando ok"


class FileSink(Sink):
    name = "arquivo"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def send(self, text: str) -> str:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(text.rstrip() + "\n\n")
            return f"arquivo ok ({self.path.name})"
        except OSError as exc:
            return f"arquivo falhou ({exc.strerror or exc})"


class Notifier:
    def __init__(self, settings: Settings, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("wedrop")
        self.sinks: List[Sink] = []
        if settings.webhook_url:
            self.sinks.append(WebhookSink(settings.webhook_url))
        if settings.notify_command:
            self.sinks.append(CommandSink(settings.notify_command))
        if settings.notify_file:
            self.sinks.append(FileSink(settings.notify_file))

    @property
    def configured(self) -> bool:
        return bool(self.sinks)

    def send(self, text: str) -> List[str]:
        """Sempre registra no log; devolve o resultado de cada sink."""
        first_line = text.strip().splitlines()[0] if text.strip() else "(vazio)"
        self.log.warning("ALERTA %s", first_line)
        results: List[str] = []
        for sink in self.sinks:
            outcome = sink.send(text)
            results.append(outcome)
            (self.log.info if outcome.endswith("ok") or outcome.endswith(")") else self.log.warning)(
                "aviso via %s: %s", sink.name, outcome
            )
        return results
