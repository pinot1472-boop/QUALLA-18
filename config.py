"""Configuración del bot. Todo se controla con variables de entorno (ver env.example).

Los valores por defecto son los mismos que los inputs del script de TradingView.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

TF_SECONDS: Dict[str, int] = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


class ConfigError(Exception):
    """Valor de configuración inválido o ausente."""


@dataclass(frozen=True)
class Config:
    # ── Telegram ──────────────────────────────────────────────
    telegram_token: str = ""
    telegram_chat_ids: Tuple[str, ...] = ()
    dry_run: bool = False  # True = no envía a Telegram, solo escribe en el log
    timezone: str = "UTC"  # solo para mostrar la hora local en los mensajes

    # ── General / sesión (grupo "General" y "Sesión" del Pine) ─
    timeframe: str = "5m"
    active_setup: str = "BOTH"  # BOTH | EP_LONG | PARABOLIC_SHORT
    sess_offset: int = 0  # minutos desde el inicio del día (00:00 UTC) hasta la "apertura"
    or_minutes: int = 15
    skip_first_min: int = 5
    window_min: int = 1080  # 0 = todo el día
    day_len: int = 1440
    max_trades_day: int = 2
    day_offset_min: int = -1  # -1 = autodetectar con las velas diarias de BingX (normalmente 0 = 00:00 UTC)

    # ── Riesgo ────────────────────────────────────────────────
    equity: float = 10000.0  # capital de referencia para dimensionar (no se compone)
    risk_pct_ep: float = 0.35
    risk_pct_short: float = 0.25
    max_pos_pct: float = 22.0
    max_adr_mult: float = 1.3
    stop_mode: str = "DAY"  # DAY | OR | ATR
    sl_buf: float = 0.1

    # ── EP Long ───────────────────────────────────────────────
    ep_min_change: float = 7.0
    ep_min_adr: float = 3.5
    ep_min_rvol: float = 1.5
    ep_trigger: str = "ORH"  # ORH | VWAP_EMA | HH

    # ── Parabolic Short ───────────────────────────────────────
    ps_min_gain: float = 50.0
    ps_lookback: int = 15
    ps_min_green: int = 3
    ps_min_ext: float = 25.0
    ps_trigger: str = "ORL"  # ORL | FIRST_RED | VWAP_FAIL

    # ── Gestión ───────────────────────────────────────────────
    partial_r: float = 1.8
    partial_pct: float = 50.0
    use_trail_ema: bool = True
    trail_ema_len: int = 10  # 10 | 20
    max_hold_bars: int = 0  # 0 = desactivado

    # ── Bot / infraestructura ─────────────────────────────────
    rate_per_sec: float = 8.0  # peticiones/seg a BingX (límite público: 100 cada 10 s)
    concurrency: int = 8
    scan_delay_sec: float = 6.0  # espera tras el cierre de vela antes de escanear
    max_signal_age_bars: float = 2.0  # señales más viejas que esto se ignoran
    warmup_bars: int = 250  # velas extra para estabilizar EMA21/ATR14
    symbols_override: Tuple[str, ...] = ()  # vacío = todos los perpetuos USDT de BingX
    exclude_regex: str = r"^NC(SK|FX|CO|SI)"  # acciones/forex/materias primas de BingX
    min_daily_vol_usdt: float = 0.0  # filtro opcional de liquidez (0 = sin filtro)
    notify_armed: bool = False  # avisos "EP armado" / "Parabolic activo"
    heartbeat_hours: float = 12.0  # 0 = desactivado
    data_dir: str = "."
    log_level: str = "INFO"
    bingx_base_url: str = "https://open-api.bingx.com"

    @property
    def tf_sec(self) -> int:
        return TF_SECONDS[self.timeframe]

    @property
    def tf_ms(self) -> int:
        return self.tf_sec * 1000


# ───────────────────────── helpers de lectura ─────────────────────────


def _raw(name: str) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip().strip('"').strip("'").strip()
    return v or None


def _str(name: str, default: str) -> str:
    v = _raw(name)
    return default if v is None else v


def _float(name: str, default: float) -> float:
    v = _raw(name)
    if v is None:
        return default
    try:
        return float(v.replace(",", "."))
    except ValueError:
        raise ConfigError(f"{name}: '{v}' no es un número válido")


def _int(name: str, default: int) -> int:
    v = _raw(name)
    if v is None:
        return default
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        raise ConfigError(f"{name}: '{v}' no es un entero válido")


def _bool(name: str, default: bool) -> bool:
    v = _raw(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on", "si", "sí", "s")


def _choice(name: str, default: str, options: Tuple[str, ...], aliases: Optional[Dict[str, str]] = None) -> str:
    v = _raw(name)
    v = default if v is None else v
    key = v.upper().replace("/", "_").replace("-", "_").replace(" ", "_")
    if aliases and key in aliases:
        key = aliases[key]
    if key not in options:
        raise ConfigError(f"{name}: '{v}' no es válido. Opciones: {', '.join(options)}")
    return key


def _csv(name: str) -> Tuple[str, ...]:
    v = _raw(name)
    if not v:
        return ()
    return tuple(x.strip() for x in v.split(",") if x.strip())


def load_dotenv(path: str) -> int:
    """Carga líneas CLAVE=VALOR de un archivo .env SIN pisar variables ya definidas (las de Railway mandan).

    Devuelve cuántas variables ha cargado. Si el archivo no existe no hace nada.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return 0
    loaded = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        elif " #" in val:  # comentario al final de la línea
            val = val.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    return loaded


def load_config() -> Config:
    for base in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        load_dotenv(os.path.join(base, ".env"))
    token = _str("TELEGRAM_BOT_TOKEN", "")
    chats = _csv("TELEGRAM_CHAT_ID")
    dry = _bool("DRY_RUN", False)
    if not dry and (not token or not chats):
        raise ConfigError(
            "Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID "
            "(para probar sin Telegram usa DRY_RUN=true)."
        )

    tf = _str("TIMEFRAME", "5m").lower()
    if tf not in TF_SECONDS:
        raise ConfigError(f"TIMEFRAME: '{tf}' no válido. Opciones: {', '.join(TF_SECONDS)}")

    trail_len = _int("TRAIL_EMA_LEN", 10)
    if trail_len not in (10, 20):
        raise ConfigError("TRAIL_EMA_LEN debe ser 10 o 20")

    cfg = Config(
        telegram_token=token,
        telegram_chat_ids=chats,
        dry_run=dry,
        timezone=_str("TIMEZONE", "UTC"),
        timeframe=tf,
        active_setup=_choice("ACTIVE_SETUP", "BOTH", ("BOTH", "EP_LONG", "PARABOLIC_SHORT")),
        sess_offset=max(0, _int("SESSION_OFFSET_MIN", 0)),
        or_minutes=max(1, _int("OR_MINUTES", 15)),
        skip_first_min=max(0, _int("SKIP_FIRST_MIN", 5)),
        window_min=max(0, _int("ENTRY_WINDOW_MIN", 1080)),
        day_len=max(60, _int("DAY_LEN_MIN", 1440)),
        max_trades_day=max(1, _int("MAX_TRADES_DAY", 2)),
        day_offset_min=_int("DAY_OFFSET_MIN", -1),
        equity=max(1.0, _float("ACCOUNT_EQUITY", 10000.0)),
        risk_pct_ep=_float("RISK_PCT_EP", 0.35),
        risk_pct_short=_float("RISK_PCT_SHORT", 0.25),
        max_pos_pct=_float("MAX_POS_PCT", 22.0),
        max_adr_mult=_float("MAX_ADR_MULT", 1.3),
        stop_mode=_choice(
            "STOP_MODE", "DAY", ("DAY", "OR", "ATR"),
            {"LOW_HIGH_DEL_DÍA": "DAY", "LOW_HIGH_DEL_DIA": "DAY", "DIA": "DAY", "DÍA": "DAY",
             "LOW_HIGH_DEL_OR": "OR", "ATR_X2": "ATR"},
        ),
        sl_buf=_float("SL_BUFFER_ATR", 0.1),
        ep_min_change=_float("EP_MIN_CHANGE", 7.0),
        ep_min_adr=_float("EP_MIN_ADR", 3.5),
        ep_min_rvol=_float("EP_MIN_RVOL", 1.5),
        ep_trigger=_choice("EP_TRIGGER", "ORH", ("ORH", "VWAP_EMA", "HH")),
        ps_min_gain=_float("PS_MIN_GAIN", 50.0),
        ps_lookback=max(2, _int("PS_LOOKBACK", 15)),
        ps_min_green=_int("PS_MIN_GREEN", 3),
        ps_min_ext=_float("PS_MIN_EXT", 25.0),
        ps_trigger=_choice("PS_TRIGGER", "ORL", ("ORL", "FIRST_RED", "VWAP_FAIL")),
        partial_r=_float("PARTIAL_R", 1.8),
        partial_pct=min(90.0, max(10.0, _float("PARTIAL_PCT", 50.0))),
        use_trail_ema=_bool("USE_TRAIL_EMA", True),
        trail_ema_len=trail_len,
        max_hold_bars=max(0, _int("MAX_HOLD_BARS", 0)),
        rate_per_sec=max(0.5, _float("RATE_PER_SEC", 8.0)),
        concurrency=max(1, _int("CONCURRENCY", 8)),
        scan_delay_sec=max(0.0, _float("SCAN_DELAY_SEC", 6.0)),
        max_signal_age_bars=max(0.5, _float("MAX_SIGNAL_AGE_BARS", 2.0)),
        warmup_bars=max(60, _int("WARMUP_BARS", 250)),
        symbols_override=tuple(s.upper() for s in _csv("SYMBOLS")),
        exclude_regex=_str("EXCLUDE_REGEX", r"^NC(SK|FX|CO|SI)"),
        min_daily_vol_usdt=max(0.0, _float("MIN_DAILY_VOL_USDT", 0.0)),
        notify_armed=_bool("NOTIFY_ARMED", False),
        heartbeat_hours=max(0.0, _float("HEARTBEAT_HOURS", 12.0)),
        data_dir=_str("DATA_DIR", "."),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        bingx_base_url=_str("BINGX_BASE_URL", "https://open-api.bingx.com"),
    )
    return cfg
