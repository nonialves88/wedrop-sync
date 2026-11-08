"""Interface de linha de comando.

    wedrop-sync run     (padrão) executa a rodada
    wedrop-sync status  últimas rodadas registradas
    wedrop-sync check   valida login e integrações sem atualizar nada
    wedrop-sync notify-test  testa os canais de aviso
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .auth import AuthError, TokenProvider
from .client import ApiClient, HttpTransport, TransportError
from .config import ConfigError, Settings, load_credentials, load_settings, require_real_channels
from .items import extract_items
from .lock import RunLock, read_owner
from .notify import Notifier
from .report import (
    compact_record,
    decide_alerts,
    format_alert_message,
    format_history,
    format_run_summary,
    needs_human,
)
from .runner import Runner
from .state import append_history, configure_logging, read_history, read_json, rotate_log, write_json_atomic

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTABLE = 2
EXIT_AUTH = 3
EXIT_LOCKED = 4
EXIT_INTERRUPTED = 130


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def _common_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--version", action="version", version=f"wedrop-sync {__version__}")
    parent.add_argument("--home", help="pasta de trabalho (padrão: ~/.wedrop)")
    parent.add_argument("--env-file", dest="env_file", help="arquivo de credenciais (padrão: <home>/creds.env)")
    parent.add_argument("--webhook", help="URL que recebe um POST JSON com o aviso")
    parent.add_argument("--notify-command", dest="notify_command", help="comando que recebe o aviso no stdin")
    parent.add_argument("--notify-file", dest="notify_file", help="arquivo onde o aviso é anexado")
    parent.add_argument("--debug", action="store_true", help="mostra traceback em caso de erro")
    return parent


def build_parser() -> argparse.ArgumentParser:
    common = _common_parent()
    parser = argparse.ArgumentParser(
        prog="wedrop-sync",
        description="Sincronização de estoque WeDrop via API (sem navegador).",
    )
    parser.add_argument("--version", action="version", version=f"wedrop-sync {__version__}")
    subparsers = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)

    run_parser = subparsers.add_parser("run", parents=[common], help="executa a rodada (comando padrão)")
    run_parser.add_argument("--only", help="canais e ordem, ex.: shopee,ml1,ml2")
    run_parser.add_argument("--workers", type=int, help="atualizações em paralelo (padrão 3)")
    run_parser.add_argument("--delay", type=float, help="pausa mínima entre envios, em segundos (padrão 0.2)")
    run_parser.add_argument("--max", type=int, help="limite de itens por canal (0 = sem limite)")
    run_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="monta a fila e não envia nada")
    run_parser.add_argument("--all", dest="all_items", action="store_true", help="todos os anúncios mapeados do canal")
    run_parser.add_argument("--resume", action="store_true", help="pula itens já enviados com sucesso nesta janela")
    run_parser.add_argument("--quiet", action="store_true", help="só grava avisos no stdout")
    run_parser.add_argument("--verify-samples", dest="verify_samples", type=int, help="quantos itens reler no fim para conferir efeito")
    run_parser.add_argument("--threshold", type=int, help="compatibilidade: o filtro de estoque baixo vem do servidor")
    run_parser.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=0.0, help="segundos de espera pela trava")
    run_parser.add_argument(
        "--parallel",
        dest="parallel_channels",
        action="store_true",
        help="roda os canais ao mesmo tempo (como fazer em 3 navegadores); "
        "contas do mesmo marketplace se revezam entre si",
    )

    status_parser = subparsers.add_parser("status", parents=[common], help="últimas rodadas registradas")
    status_parser.add_argument("--json", action="store_true", help="saída em JSON")
    status_parser.add_argument("--limit", type=int, default=10)

    check_parser = subparsers.add_parser("check", parents=[common], help="valida login e integrações (não atualiza nada)")
    check_parser.add_argument("--json", action="store_true", help="saída em JSON")

    subparsers.add_parser("notify-test", parents=[common], help="envia um aviso de teste pelos canais configurados")
    subparsers.add_parser("version", help="mostra a versão")

    serve_parser = subparsers.add_parser(
        "serve", parents=[common], help="painel local: um botão de sincronizar por integração"
    )
    serve_parser.add_argument("--port", type=int, default=8790, help="porta do painel (padrão 8790)")
    serve_parser.add_argument("--bind", default="127.0.0.1", help="endereço (padrão 127.0.0.1, só esta máquina)")
    serve_parser.add_argument("--open", action="store_true", help="abre o painel no navegador")
    return parser


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings_from(args: argparse.Namespace) -> Settings:
    require_real_channels()  # nunca usar IDs de demonstração contra a API real
    collected: Dict[str, Any] = {}
    for key in (
        "home",
        "env_file",
        "only",
        "workers",
        "delay",
        "max",
        "verify_samples",
        "threshold",
        "webhook",
        "notify_command",
        "notify_file",
    ):
        value = getattr(args, key, None)
        if value is not None:
            collected[key] = value
    for flag, target in (
        ("dry_run", "dry_run"),
        ("all_items", "all_items"),
        ("resume", "resume"),
        ("quiet", "quiet"),
        ("parallel_channels", "parallel_channels"),
    ):
        if getattr(args, flag, False):
            collected[target] = True
    return load_settings(collected)


def _credentials_or_exit(settings: Settings):
    try:
        return load_credentials(settings.creds_file)
    except ConfigError as exc:
        sys.stderr.write(f"erro de configuração: {exc}\n")
        return None


def _install_signal_handlers(runner: Runner) -> None:
    state = {"count": 0}

    def handler(signum, frame):  # noqa: ARG001
        state["count"] += 1
        if state["count"] > 1:
            sys.stderr.write("\nforçando saída\n")
            raise SystemExit(EXIT_INTERRUPTED)
        runner.stop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # thread secundária / plataforma sem sinal
            pass


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    credentials = _credentials_or_exit(settings)
    if credentials is None:
        return EXIT_AUTH
    settings.ensure_dirs()

    lock = RunLock(settings.lock_path)
    if not lock.acquire(timeout=getattr(args, "lock_timeout", 0.0) or 0.0):
        owner = read_owner(settings.lock_path)
        sys.stderr.write(
            "já existe uma sincronização em andamento"
            + (f" (pid {owner})" if owner else "")
            + " — nada a fazer\n"
        )
        return EXIT_LOCKED

    try:
        rotate_log(settings.log_path, settings.prev_log_path)
        logger = configure_logging(settings.log_path, quiet=settings.quiet)
        previous = read_json(settings.last_run_json, default=None)

        runner = Runner(settings, credentials, logger=logger)
        _install_signal_handlers(runner)
        run = runner.run()

        alerts = decide_alerts(run, settings, previous=previous)
        record = run.to_dict()
        record["alerts"] = [alert.to_dict() for alert in alerts]
        try:
            write_json_atomic(settings.last_run_json, record)
            append_history(settings.history_jsonl, compact_record(run, alerts))
        except OSError as exc:
            logger.warning("não consegui gravar o estado da rodada: %s", exc)

        print(format_run_summary(run))
        if alerts:
            notifier = Notifier(settings, logger)
            notifier.send(format_alert_message(run, alerts))
            if not notifier.configured:
                logger.info("(nenhum canal de aviso configurado: use --webhook ou --notify-command)")

        if run.fatal_kind:
            return EXIT_AUTH if run.fatal_kind in ("invalid_credentials", "email_code", "bad_response") else EXIT_ERROR
        if run.interrupted:
            return EXIT_INTERRUPTED
        return EXIT_NOTABLE if needs_human(alerts) else EXIT_OK
    finally:
        lock.release()


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    records = read_history(settings.history_jsonl)
    if not records:
        last = read_json(settings.last_run_json, default=None)
        if last:
            records = [last]
    if getattr(args, "json", False):
        print(json.dumps(records[-int(args.limit or 10):], ensure_ascii=False, indent=2))
    else:
        print(format_history(records, limit=int(getattr(args, "limit", 10) or 10)))
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    credentials = _credentials_or_exit(settings)
    if credentials is None:
        return EXIT_AUTH

    transport = HttpTransport(
        settings.api_base,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        backoff=settings.retry_backoff,
        default_retry_after=settings.default_retry_after,
    )
    tokens = TokenProvider(transport, credentials)
    api = ApiClient(transport, tokens, read_attempts=settings.read_attempts)
    report: Dict[str, Any] = {"ok": False, "channels": [], "errors": []}

    try:
        me = api.get("/me", attempts=settings.read_attempts)
        if me.status in (401, 403):
            report["errors"].append("login recusado (HTTP %s)" % me.status)
            return _print_check(report, args, EXIT_AUTH)
        user_id = None
        if isinstance(me.data, dict):
            for key in ("userId", "user_id", "id"):
                if me.data.get(key) is not None:
                    user_id = me.data.get(key)
                    break
        report["user_id"] = user_id
        report["login"] = "ok"

        integrations = api.get("/my-integrations", attempts=settings.read_attempts)
        entries = extract_items(integrations.data)
        known: Dict[str, Any] = {}
        for entry in entries:
            for key in ("integrationId", "integration_id", "id"):
                if entry.get(key) is not None:
                    known[str(entry[key])] = entry
                    break

        report["integrations_found"] = sorted(known)
        for channel in settings.order:
            entry = known.get(str(channel.integration_id))
            found = entry is not None
            status = str(entry.get("status") or entry.get("situation") or "") if entry else ""
            report["channels"].append(
                {
                    "key": channel.key,
                    "name": channel.name,
                    "integration_id": channel.integration_id,
                    "found": found,
                    "status": status,
                }
            )
        report["ok"] = all(item["found"] for item in report["channels"])
        return _print_check(report, args, EXIT_OK if report["ok"] else EXIT_NOTABLE)
    except AuthError as exc:
        report["errors"].append(f"autenticação: {exc}")
        return _print_check(report, args, EXIT_AUTH)
    except TransportError as exc:
        report["errors"].append(f"rede: {exc}")
        return _print_check(report, args, EXIT_ERROR)
    finally:
        transport.close()


def _print_check(report: Dict[str, Any], args: argparse.Namespace, code: int) -> int:
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code
    print(f"api: {report.get('login', 'sem login')}")
    print(f"userId: {report.get('user_id', '?')}")
    if report.get("integrations_found"):
        print(f"integrações na conta: {', '.join(report['integrations_found'])}")
    for channel in report.get("channels", []):
        mark = "OK " if channel["found"] else "!! "
        status = f" status={channel['status']}" if channel["status"] else ""
        print(f"  {mark}{channel['key']:<7} integrationId={channel['integration_id']}{status}")
    for error in report.get("errors", []):
        print(f"  erro: {error}")
    return code


def cmd_notify_test(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    notifier = Notifier(settings)
    if not notifier.configured:
        print("nenhum canal de aviso configurado (--webhook / --notify-command / --notify-file)")
        return EXIT_NOTABLE
    results = notifier.send("✅ teste de aviso do wedrop-sync")
    for result in results:
        print(result)
    return EXIT_OK if all(not r.split()[0].endswith("falhou") for r in results) else EXIT_ERROR


def cmd_serve(args: argparse.Namespace) -> int:
    """Painel local com um botão por integração (Shopee / ML1 / ML2)."""
    from .web import WebServer  # import tardio: o resto da CLI não precisa disto

    settings = _settings_from(args)
    credentials = _credentials_or_exit(settings)
    if credentials is None:
        return EXIT_AUTH
    settings.ensure_dirs()
    logger = configure_logging(settings.log_path, quiet=True)

    server = WebServer(settings, credentials, host=args.bind, port=args.port, logger=logger)
    print(f"\n  Painel: {server.url}\n  conta: {settings.creds_file}  ·  Ctrl+C para parar\n")
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando painel…")
    finally:
        server.stop()
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["run"] + argv  # `wedrop-sync --dry-run` == `wedrop-sync run --dry-run`

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"wedrop-sync {__version__}")
        return EXIT_OK

    commands = {
        "status": cmd_status,
        "check": cmd_check,
        "notify-test": cmd_notify_test,
        "serve": cmd_serve,
    }
    try:
        return commands.get(args.command, cmd_run)(args)
    except ConfigError as exc:
        sys.stderr.write(f"erro de configuração: {exc}\n")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - nunca deixar a cron com traceback cru
        if getattr(args, "debug", False):
            raise
        sys.stderr.write(f"erro inesperado: {type(exc).__name__}: {exc}\n")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
