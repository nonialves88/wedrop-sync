"""Demonstração offline: roda uma rodada completa contra a API falsa.

Serve para conferir o comportamento (resumo, alertas, arquivos gerados) sem
credencial nenhuma e sem tocar em anúncio de verdade:

    python scripts/demo.py            # rodada normal
    python scripts/demo.py --full     # modo completo (todos os mapeados)
    python scripts/demo.py --broken   # simula integração sem autorização

Nada aqui fala com a internet.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from tests.mockapi import FakeWeDrop  # noqa: E402

from wedrop_sync import cli  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(description="rodada de demonstração (offline)")
    parser.add_argument("--full", action="store_true", help="todos os anúncios mapeados")
    parser.add_argument("--broken", action="store_true", help="ML1 perde a autorização em tudo")
    parser.add_argument("--home", default=None, help="pasta de trabalho da demo")
    args = parser.parse_args()

    fake = FakeWeDrop()
    base_url = fake.start()
    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="wedrop-demo-"))

    if args.broken:
        for item in fake.items_by_channel[10002]:
            fake.behavior(item["id"], "auth_loss")

    argv = ["run", "--home", str(home), "--workers", "3", "--delay", "0", "--verify-samples", "5"]
    if args.full:
        argv.append("--all")

    print(f"API falsa em {base_url} · pasta {home}\n")
    try:
        import os

        os.environ.setdefault("WEDROP_ALLOW_DEMO_CHANNELS", "1")
        os.environ.setdefault("WEDROP_API_BASE", base_url)
        os.environ.setdefault("WEDROP_EMAIL", "demo@loja.com")
        os.environ.setdefault("WEDROP_PASSWORD", "demo")
        os.environ.setdefault("WEDROP_BREAKER", "6")
        code = cli.main(argv)
    finally:
        fake.stop()

    print("\nArquivos gerados:")
    for path in sorted(home.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(home)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
