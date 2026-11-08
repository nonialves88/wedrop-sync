"""Configuração, canais e credenciais do sincronizador WeDrop (API v3).

Regra de ouro deste módulo: **a senha nunca sai daqui**. Nenhum objeto desta
biblioteca imprime, loga ou serializa a senha; `Credentials.__repr__` mascara.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

API_BASE = "https://api.wedrop.com.br/v3/api"
DEFAULT_HOME = Path.home() / ".wedrop"


class ConfigError(RuntimeError):
    """Erro de configuração — mensagem sempre segura para log."""


# --------------------------------------------------------------------------- #
# Canais
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    integration_id: int
    note: str = ""
    #: marketplace dono do rate limit (Shopee e ML são orçamentos separados;
    #: as duas contas do ML dividem o mesmo)
    marketplace: str = ""

    def __str__(self) -> str:  # usado nos relatórios
        return self.key

    @property
    def lane(self) -> str:
        """Faixa de execução paralela: canais da mesma faixa se revezam."""
        return self.marketplace or self.key


# IDs de demonstração (fictícios): servem para testes e para o `scripts/demo.py`.
# Os IDs REAIS nunca ficam no código — vêm de `channels.json` (ver `load_channels`),
# que no GitHub Actions é decifrado de `config/channels.enc` em tempo de execução.
_DEMO_CHANNELS: list = [
    {"key": "shopee", "name": "Shopee", "id": 10001, "marketplace": "shopee"},
    {"key": "ml1", "name": "Mercado Livre 1", "id": 10002, "marketplace": "mercadolivre"},
    {"key": "ml2", "name": "Mercado Livre 2", "id": 10003, "marketplace": "mercadolivre"},
    {"key": "bling", "name": "Bling", "id": 10004, "marketplace": "bling", "ignored": True},
]

CHANNELS_FILE_ENV = "WEDROP_CHANNELS_FILE"
ALLOW_DEMO_ENV = "WEDROP_ALLOW_DEMO_CHANNELS"


def _build_channels(items: Sequence[Mapping[str, object]]) -> Tuple[dict, Tuple[str, ...], Tuple[str, ...]]:
    channels: dict[str, Channel] = {}
    order: list = []
    ignored: list = []
    for entry in items:
        try:
            key = str(entry["key"]).strip().lower().replace("-", "")  # "ml-1" == "ml1"
            integration_id = int(entry["id"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("channels.json inválido: cada canal precisa de 'key' e 'id' numérico") from exc
        if not key or key in channels:
            raise ConfigError("channels.json inválido: 'key' vazia ou repetida")
        channels[key] = Channel(
            key,
            str(entry.get("name") or key),
            integration_id,
            str(entry.get("note") or ""),
            str(entry.get("marketplace") or ""),
        )
        (ignored if (entry.get("ignored") or entry.get("active") is False) else order).append(key)
    if not order:
        raise ConfigError("channels.json sem nenhum canal utilizável")
    return channels, tuple(order), tuple(ignored)


def load_channels(
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[dict, Tuple[str, ...], Tuple[str, ...], bool]:
    """Devolve (canais, ordem padrão, sempre ignorados, é_demo).

    Fontes, na ordem: `WEDROP_CHANNELS_FILE` > `<home>/channels.json` > demonstração.
    Formato: `{"channels": [{"key","name","id","marketplace","note","ignored"}]}`.
    """
    import json

    env = os.environ if env is None else env
    explicit = env.get(CHANNELS_FILE_ENV)
    home = Path(env.get("WEDROP_HOME") or DEFAULT_HOME).expanduser()
    path = Path(explicit).expanduser() if explicit else home / "channels.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"não consegui ler {path.name}: {type(exc).__name__}") from exc
        items = data.get("channels") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ConfigError("channels.json inválido: esperado {\"channels\": [...]}")
        channels, order, ignored = _build_channels(items)
        return channels, order, ignored, False
    # Arquivo ausente NÃO falha no import: o cofre (vault) vive neste pacote e precisa
    # poder rodar ANTES de o channels.json existir. O erro sai em require_real_channels().
    channels, order, ignored = _build_channels(_DEMO_CHANNELS)
    return channels, order, ignored, True


CHANNELS, DEFAULT_ORDER, ALWAYS_IGNORED, USING_DEMO_CHANNELS = load_channels()


def require_real_channels(env: Optional[Mapping[str, str]] = None) -> None:
    """Impede rodar contra a API real com IDs de demonstração."""
    env = os.environ if env is None else env
    if USING_DEMO_CHANNELS and env.get(ALLOW_DEMO_ENV) != "1":
        if env.get(CHANNELS_FILE_ENV):
            raise ConfigError(f"{CHANNELS_FILE_ENV} aponta para um arquivo que não existe")
        raise ConfigError(
            "nenhum channels.json encontrado: crie <home>/channels.json (veja "
            "config/channels.example.json) ou defina WEDROP_CHANNELS_FILE"
        )


def parse_order(value: str) -> Tuple[Channel, ...]:
    """Converte "shopee,ml1,ml2" (ou "shopee ml1 ml2") em canais na ordem dada."""
    raw = value.replace(";", ",").replace(" ", ",")
    keys = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not keys:
        raise ConfigError("lista de canais vazia; use por exemplo --only shopee,ml1,ml2")

    unknown = [k for k in keys if k not in CHANNELS]
    if unknown:
        raise ConfigError(
            f"canal desconhecido: {', '.join(unknown)}. Válidos: {', '.join(CHANNELS)}"
        )

    kept, seen = [], set()
    ignored = []
    for key in keys:
        if key in ALWAYS_IGNORED:
            ignored.append(key)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(CHANNELS[key])

    if ignored:
        # Silencioso de propósito: o Bling fica fora do fluxo por decisão de produto.
        pass
    if not kept:
        raise ConfigError("nenhum canal utilizável na lista (o Bling é ignorado)")
    return tuple(kept)


def default_order() -> Tuple[Channel, ...]:
    return tuple(CHANNELS[k] for k in DEFAULT_ORDER)


# --------------------------------------------------------------------------- #
# Credenciais
# --------------------------------------------------------------------------- #
_EMAIL_KEYS = ("wedrop_email", "email", "usuario", "user", "login")
_PASSWORD_KEYS = ("wedrop_password", "password", "senha", "pass", "pwd")


def read_env_file(path: Path) -> dict:
    """Lê um .env tolerante: `KEY=valor`, `KEY = valor`, `export KEY=...`, aspas e comentários."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return values
    except OSError as exc:  # permissão, drive removido etc.
        raise ConfigError(f"não consegui ler {path.name}: {exc.strerror or exc}") from exc

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("\ufeff").lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # comentário no fim da linha
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    source: str = "creds.env"

    def __repr__(self) -> str:  # nunca vaza a senha, nem em traceback
        return f"Credentials(email={mask_email(self.email)}, source={self.source!r})"

    __str__ = __repr__


def mask_email(email: str) -> str:
    if not email:
        return "(vazio)"
    if "@" not in email:
        return email[:2] + "***"
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"


def load_credentials(creds_file: Path, env: Optional[Mapping[str, str]] = None) -> Credentials:
    """CLI/env têm precedência sobre o arquivo. Nunca levanta exceção com a senha na mensagem."""
    env = os.environ if env is None else env
    file_values = read_env_file(creds_file)

    def pick(keys: Sequence[str]) -> str:
        for key in keys:  # variáveis de ambiente ganham
            value = env.get(key.upper()) or env.get(key)
            if value:
                return value.strip()
        for key in keys:
            if file_values.get(key):
                return file_values[key].strip()
        return ""

    email = pick(_EMAIL_KEYS)
    password = pick(_PASSWORD_KEYS)

    if not email or not password:
        raise ConfigError(
            f"credenciais ausentes: preencha WEDROP_EMAIL e WEDROP_PASSWORD em "
            f"{creds_file} (ou nas variáveis de ambiente de mesmo nome)"
        )
    source = "ambiente" if (env.get("WEDROP_EMAIL") and env.get("WEDROP_PASSWORD")) else str(creds_file)
    return Credentials(email=email, password=password, source=source)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int, env: Mapping[str, str]) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        raise ConfigError(f"{name} precisa ser número, veio {raw!r}")


def _env_float(name: str, default: float, env: Mapping[str, str]) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} precisa ser número, veio {raw!r}")


def _env_bool(name: str, default: bool, env: Mapping[str, str]) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "sim")


@dataclass
class Settings:
    """Tudo que uma rodada precisa saber. Imutável na prática: monte e não mexa."""

    home: Path
    api_base: str
    order: Tuple[Channel, ...]
    creds_file: Path
    workers: int = 3
    delay: float = 0.2
    #: limite de unidades do critério "estoque baixo" (o painel usa ≤10)
    low_stock_max: int = 10
    max_items: int = 0
    dry_run: bool = False
    all_items: bool = False
    page_size: int = 0
    #: regra do marketplace: update-stock nunca em paralelo (ver ratelimit.py)
    serialize_updates: bool = True
    #: rodar os canais ao mesmo tempo (como fazer o processo em 3 navegadores):
    #: uma faixa por marketplace, então ML CPF e ML PJ se revezam entre si
    parallel_channels: bool = False
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    read_attempts: int = 3
    update_attempts: int = 2
    retry_backoff: float = 2.0
    default_retry_after: float = 60.0
    verify_samples: int = 25
    breaker_threshold: int = 12
    notify_min_errors: int = 15
    notify_error_rate: float = 0.10
    resume: bool = False
    quiet: bool = False
    progress_every: float = 60.0
    webhook_url: Optional[str] = None
    notify_command: Optional[str] = None
    notify_file: Optional[Path] = None
    threshold: Optional[int] = None  # compatibilidade: o filtro agora vem do servidor

    # ---- caminhos derivados ----
    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def lock_path(self) -> Path:
        return self.home / ".sync.lock"

    @property
    def log_path(self) -> Path:
        return self.home / "last-run.log"

    @property
    def prev_log_path(self) -> Path:
        return self.home / "prev-run.log"

    @property
    def last_run_json(self) -> Path:
        return self.state_dir / "last-run.json"

    @property
    def history_jsonl(self) -> Path:
        return self.state_dir / "history.jsonl"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.state_dir, self.log_dir, self.artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)

    def channel_keys(self) -> Tuple[str, ...]:
        return tuple(c.key for c in self.order)


def load_settings(
    args: Optional[Mapping[str, object]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Precedência: argumentos de linha de comando > variáveis de ambiente > padrões."""
    args = dict(args or {})
    env = os.environ if env is None else env

    home = Path(
        (args.get("home") or env.get("WEDROP_HOME") or DEFAULT_HOME).__str__()
    ).expanduser()

    order_arg = args.get("only")
    order = parse_order(str(order_arg)) if order_arg else (
        default_order() if not env.get("WEDROP_ONLY") else parse_order(env["WEDROP_ONLY"])
    )

    creds_explicito = args.get("env_file") or env.get("WEDROP_ENV_FILE")
    creds_file = Path(
        str(creds_explicito or (home / "creds.env"))
    ).expanduser()
    if not creds_explicito and not creds_file.exists():
        # A credencial canônica fica FORA da pasta do projeto (e do OneDrive):
        # ~/.wedrop/creds.env com permissão só do dono.
        padrao = DEFAULT_HOME / "creds.env"
        if padrao.exists():
            creds_file = padrao

    def arg_int(name: str, env_name: str, default: int) -> int:
        value = args.get(name)
        if value is not None:
            return int(value)
        return _env_int(env_name, default, env)

    def arg_float(name: str, env_name: str, default: float) -> float:
        value = args.get(name)
        if value is not None:
            return float(value)
        return _env_float(env_name, default, env)

    workers = arg_int("workers", "WEDROP_WORKERS", 3)
    delay = arg_float("delay", "WEDROP_DELAY", 0.2)
    max_items = arg_int("max", "WEDROP_MAX", 0)
    all_items = bool(args.get("all_items")) or _env_bool("WEDROP_ALL", False, env)
    dry_run = bool(args.get("dry_run")) or _env_bool("WEDROP_DRY_RUN", False, env)
    resume = bool(args.get("resume")) or _env_bool("WEDROP_RESUME", False, env)
    quiet = bool(args.get("quiet")) or _env_bool("WEDROP_QUIET", False, env)

    if workers < 1:
        raise ConfigError("--workers precisa ser >= 1")
    if delay < 0:
        raise ConfigError("--delay não pode ser negativo")

    webhook = args.get("webhook") or env.get("WEDROP_WEBHOOK_URL") or None
    command = args.get("notify_command") or env.get("WEDROP_NOTIFY_COMMAND") or None
    notify_file = args.get("notify_file") or env.get("WEDROP_NOTIFY_FILE") or None

    return Settings(
        home=home,
        api_base=str(args.get("api_base") or env.get("WEDROP_API_BASE") or API_BASE),
        order=order,
        creds_file=creds_file,
        workers=workers,
        delay=delay,
        low_stock_max=max(0, arg_int("low_stock_max", "WEDROP_LOW_STOCK_MAX", 10)),
        max_items=max(max_items, 0),
        dry_run=dry_run,
        all_items=all_items,
        serialize_updates=_env_bool("WEDROP_SERIALIZE_UPDATES", True, env),
        parallel_channels=bool(args.get("parallel_channels")) or _env_bool("WEDROP_PARALLEL", False, env),
        page_size=max(0, arg_int("page_size", "WEDROP_PAGE_SIZE", 0)),
        connect_timeout=arg_float("connect_timeout", "WEDROP_CONNECT_TIMEOUT", 10.0),
        read_timeout=arg_float("read_timeout", "WEDROP_READ_TIMEOUT", 30.0),
        read_attempts=max(1, arg_int("read_attempts", "WEDROP_READ_ATTEMPTS", 3)),
        update_attempts=max(1, arg_int("update_attempts", "WEDROP_UPDATE_ATTEMPTS", 2)),
        default_retry_after=arg_float("retry_after", "WEDROP_RETRY_AFTER", 60.0),
        verify_samples=max(0, arg_int("verify_samples", "WEDROP_VERIFY_SAMPLES", 25)),
        breaker_threshold=max(1, arg_int("breaker", "WEDROP_BREAKER", 12)),
        notify_min_errors=max(1, arg_int("notify_min_errors", "WEDROP_NOTIFY_MIN_ERRORS", 15)),
        notify_error_rate=arg_float("notify_error_rate", "WEDROP_NOTIFY_ERROR_RATE", 0.10),
        resume=resume,
        quiet=quiet,
        progress_every=max(5.0, _env_float("WEDROP_PROGRESS_EVERY", 60.0, env)),
        webhook_url=str(webhook) if webhook else None,
        notify_command=str(command) if command else None,
        notify_file=Path(str(notify_file)).expanduser() if notify_file else None,
        threshold=int(args["threshold"]) if args.get("threshold") is not None else None,
    )


