"""Cofre de arquivos: AES-256-GCM com chave derivada por scrypt.

Para que serve (repositório PÚBLICO):
  * ``config/channels.enc`` — IDs reais das integrações, versionados já cifrados;
  * artefatos de rodada (``last-run.json``, log) enviados cifrados ao GitHub Actions.

A senha-mestra vem SEMPRE da variável de ambiente ``VAULT_KEY`` (secret do
GitHub). Nunca por argumento de linha de comando (apareceria em ``ps``/logs).

Uso:
    python -m wedrop_sync.vault keygen
    python -m wedrop_sync.vault encrypt ENTRADA SAIDA.enc
    python -m wedrop_sync.vault decrypt SAIDA.enc ENTRADA
    python -m wedrop_sync.vault decrypt SAIDA.enc -          # imprime no stdout

Formato (texto, seguro para git):
    WDV1.<n>.<r>.<p>.<base64 de salt(16) | nonce(12) | ciphertext+tag>
O cabeçalho (versão + parâmetros do scrypt) é autenticado como AAD.

Aviso: como o arquivo cifrado fica público, um atacante pode tentar adivinhar a
senha offline. Use uma senha LONGA E ALEATÓRIA (`keygen` gera 256 bits).
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path
from typing import Optional

KEY_ENV = "VAULT_KEY"
MAGIC = "WDV1"
SCRYPT_N = 2**15  # ~32 MiB; custo de CPU/mem alto o bastante e roda em qualquer runner
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE = 20


class VaultError(RuntimeError):
    """Mensagem sempre segura para log (nunca inclui senha nem conteúdo)."""


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise VaultError("falta a biblioteca 'cryptography' (pip install cryptography)") from exc
    return AESGCM, Scrypt


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    _, Scrypt = _crypto()
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _header(n: int, r: int, p: int) -> str:
    return f"{MAGIC}.{n}.{r}.{p}"


def _check_passphrase(passphrase: Optional[str]) -> str:
    if not passphrase:
        raise VaultError(f"defina a variável de ambiente {KEY_ENV} com a senha-mestra do cofre")
    if len(passphrase) < MIN_PASSPHRASE:
        raise VaultError(f"{KEY_ENV} muito curta (mínimo {MIN_PASSPHRASE} caracteres); use `keygen`")
    return passphrase


def encrypt_bytes(data: bytes, passphrase: Optional[str]) -> str:
    passphrase = _check_passphrase(passphrase)
    AESGCM, _ = _crypto()
    salt, nonce = os.urandom(16), os.urandom(12)
    header = _header(SCRYPT_N, SCRYPT_R, SCRYPT_P)
    key = _derive(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    blob = AESGCM(key).encrypt(nonce, data, header.encode("ascii"))
    return f"{header}.{base64.b64encode(salt + nonce + blob).decode('ascii')}\n"


def decrypt_bytes(text: str, passphrase: Optional[str]) -> bytes:
    passphrase = _check_passphrase(passphrase)
    AESGCM, _ = _crypto()
    parts = text.strip().split(".", 4)
    if len(parts) != 5 or parts[0] != MAGIC:
        raise VaultError("arquivo não está no formato do cofre (WDV1)")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        raw = base64.b64decode(parts[4], validate=True)
    except ValueError as exc:
        raise VaultError("cofre corrompido") from exc
    if n > 2**20 or r > 32 or p > 8 or len(raw) < 16 + 12 + 16:
        raise VaultError("parâmetros do cofre fora do aceitável")
    salt, nonce, blob = raw[:16], raw[16:28], raw[28:]
    key = _derive(passphrase, salt, n, r, p)
    try:
        return AESGCM(key).decrypt(nonce, blob, _header(n, r, p).encode("ascii"))
    except Exception as exc:  # InvalidTag: senha errada OU arquivo adulterado
        raise VaultError("não consegui abrir o cofre: senha errada ou arquivo alterado") from exc


def encrypt_file(src: Path, dst: Path, passphrase: Optional[str]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(encrypt_bytes(Path(src).read_bytes(), passphrase), encoding="ascii")


def decrypt_file(src: Path, dst: Optional[Path], passphrase: Optional[str]) -> None:
    data = decrypt_bytes(Path(src).read_text(encoding="ascii"), passphrase)
    if dst is None:
        sys.stdout.buffer.write(data)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 0600: só o dono lê (no runner do Actions é descartado ao fim do job)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "keygen":
            print(secrets.token_urlsafe(32))
            return 0
        if cmd in {"encrypt", "decrypt"} and len(rest) == 2:
            passphrase = os.environ.get(KEY_ENV)
            if cmd == "encrypt":
                encrypt_file(Path(rest[0]), Path(rest[1]), passphrase)
            else:
                decrypt_file(Path(rest[0]), None if rest[1] == "-" else Path(rest[1]), passphrase)
            return 0
    except (VaultError, OSError) as exc:
        sys.stderr.write(f"vault: {exc}\n")
        return 1
    sys.stderr.write("uso: python -m wedrop_sync.vault {keygen|encrypt IN OUT|decrypt IN OUT|-}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
