#!/usr/bin/env python3
"""Cria o cofre do repositório, NA SUA MÁQUINA (a chave nunca passa por chat).

    python scripts/setup_vault.py ~/.wedrop/channels.json

1. gera uma senha-mestra aleatória (256 bits) e a mostra UMA vez;
2. cifra o seu channels.json real em config/channels.enc (esse arquivo pode ser público);
3. diz o que cadastrar nos Secrets do GitHub.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api-sync"))

from wedrop_sync import vault  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).expanduser()
    if not src.is_file():
        print(f"não encontrei {src}")
        return 1
    key = os.environ.get(vault.KEY_ENV) or vault.secrets.token_urlsafe(32)
    reused = bool(os.environ.get(vault.KEY_ENV))
    out = ROOT / "config" / "channels.enc"
    vault.encrypt_file(src, out, key)
    check = vault.decrypt_bytes(out.read_text(encoding="ascii"), key)
    assert check == src.read_bytes(), "falha na conferência do cofre"
    print(f"OK: {out.relative_to(ROOT)} gravado e conferido.\n")
    if not reused:
        print("SENHA-MESTRA (copie agora; não será mostrada de novo):\n")
        print(f"    {key}\n")
    print("Próximos passos:")
    print("  1. GitHub > Settings > Secrets and variables > Actions > New secret")
    print("     VAULT_KEY = a senha acima  (guarde também no seu gerenciador de senhas)")
    print("     WEDROP_EMAIL, WEDROP_PASSWORD = login da WeDrop")
    print("     (opcional) ZAPI_INSTANCE, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN, ALERT_PHONE")
    print("  2. git add config/channels.enc && git commit && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
