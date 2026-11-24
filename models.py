"""Estructuras de datos compartidas por todos los módulos."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

DAY_MS = 86_400_000


def day_start(ts_ms: int, off_ms: int = 0) -> int:
    """Inicio del día al que pertenece ts_ms (equivale a time("D") del Pine).

    Por defecto 00:00 UTC; off_ms desplaza la frontera (se autodetecta con las velas diarias de BingX).
    """
    return ts_ms - ((ts_ms - off_ms) % DAY_MS)


@dataclass
class Candles:
    """Velas en formato columnar, ordenadas de más antigua a más reciente."""

    ts: List[int] = field(default_factory=list)  # hora de APERTURA en ms
    o: List[float] = field(default_factory=list)
    h: List[float] = field(default_factory=list)
    l: List[float] = field(default_factory=list)
    c: List[float] = field(default_factory=list)
    v: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.ts)

    def closed_only(self, now_ms: int, tf_ms: int) -> "Candles":
        """Descarta las velas finales que todavía no han cerrado."""
        k = len(self.ts)
        while k > 0 and self.ts[k - 1] + tf_ms > now_ms:
            k -= 1
        if k == len(self.ts):
            return self
        return Candles(self.ts[:k], self.o[:k], self.h[:k], self.l[:k], self.c[:k], self.v[:k])


@dataclass
class SymMeta:
    """Datos del contrato en BingX (para redondear cantidades y precios)."""

    symbol: str
    asset: str = ""
    price_prec: Optional[int] = None
    qty_prec: Optional[int] = None
    min_qty: Optional[float] = None


@dataclass
class DailyCtx:
    """Valores diarios 'confirmados' (día anterior), igual que f_daily()[1] del Pine."""

    prior_close: float
    adr: Optional[float]
    avg_vol: Optional[float]
    ema10: Optional[float]
    ema20: Optional[float]
    green_streak: Optional[int]
    base_low: Optional[float]


@dataclass
class Trade:
    """Posición VIRTUAL que el bot sigue para replicar la gestión de la estrategia."""

    symbol: str
    setup: str  # "EP Long" | "Parabolic Short"
    side: str  # "long" | "short"
    entry: float
    init_stop: float
    stop: float  # stop vigente (pasa a BE tras el parcial)
    risk: float  # distancia inicial entrada-stop (en precio)
    tp1: float
    qty: float
    risk_cash: float
    entry_ts: int  # apertura de la vela de señal
    entry_day: int
    tf: str = ""
    price_prec: Optional[int] = None  # decimales del contrato (solo para mostrar precios)
    partial_taken: bool = False
    remaining: float = 1.0
    realized_r: float = 0.0


@dataclass
class SymState:
    """Estado persistente por símbolo."""

    last_ts: int = 0  # apertura de la última vela ya procesada
    trades_day: int = 0
    trades_count: int = 0
    armed_ep_day: int = 0
    armed_ps_day: int = 0
    pos: Optional[Trade] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SymState":
        d = dict(d)
        pos = d.pop("pos", None)
        valid = SymState.__dataclass_fields__
        st = SymState(**{k: v for k, v in d.items() if k in valid and k != "pos"})
        if pos:
            tf = Trade.__dataclass_fields__
            st.pos = Trade(**{k: v for k, v in pos.items() if k in tf})
        return st


@dataclass
class Event:
    """Algo que debe notificarse (entrada, parcial, salida, aviso de contexto...)."""

    kind: str  # entry | tp1 | stop | be | trail | maxbars | armed_ep | armed_ps
    symbol: str
    ts: int  # apertura de la vela que lo provocó
    data: Dict[str, Any] = field(default_factory=dict)
