"""Estado persistente en un JSON (escritura atómica).

En Railway el disco es efímero: para conservar posiciones virtuales y última vela procesada
entre despliegues, añade un Volume y apunta DATA_DIR a su ruta (p. ej. /data).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List

from models import DAY_MS, SymState, Trade

log = logging.getLogger("store")

VERSION = 1


def _new_stats() -> Dict[str, Any]:
    return {"closed": 0, "wins": 0, "sum_r": 0.0, "day": 0, "sig_ep": 0, "sig_ps": 0}


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir or "."
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "state.json")
        self.symbols: Dict[str, SymState] = {}
        self.stats: Dict[str, Any] = _new_stats()
        self._load()

    # ── carga / guardado ───────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self.path):
            log.info("Sin estado previo en %s: se empieza desde cero", self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for sym, d in (raw.get("symbols") or {}).items():
                self.symbols[sym] = SymState.from_dict(d)
            stats = raw.get("stats") or {}
            self.stats.update({k: stats[k] for k in self.stats if k in stats})
            log.info("Estado cargado: %d símbolos, %d posiciones virtuales abiertas",
                     len(self.symbols), len(self.open_positions()))
        except Exception as e:  # noqa: BLE001
            bad = f"{self.path}.corrupto-{int(time.time())}"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            self.symbols = {}
            self.stats = _new_stats()
            log.error("state.json ilegible (%s). Se guardó como %s y se empieza desde cero", e, bad)

    def save(self) -> None:
        payload = {
            "version": VERSION,
            "saved_at": int(time.time()),
            "symbols": {s: st.to_dict() for s, st in self.symbols.items()},
            "stats": self.stats,
        }
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("No se pudo guardar el estado: %s", e)

    # ── acceso ─────────────────────────────────────────────────

    def sym(self, symbol: str) -> SymState:
        st = self.symbols.get(symbol)
        if st is None:
            st = SymState()
            self.symbols[symbol] = st
        return st

    def has_pos(self, symbol: str) -> bool:
        st = self.symbols.get(symbol)
        return st is not None and st.pos is not None

    def open_positions(self) -> List[Trade]:
        return [st.pos for st in self.symbols.values() if st.pos is not None]

    def prune(self, keep: Iterable[str]) -> None:
        """Elimina el estado de símbolos que ya no existen y no tienen posición abierta."""
        keep_set = set(keep)
        for s in list(self.symbols):
            if s not in keep_set and self.symbols[s].pos is None:
                del self.symbols[s]

    # ── estadísticas ───────────────────────────────────────────

    def _roll_day(self, ts_ms: int) -> None:
        d = ts_ms - ts_ms % DAY_MS
        if self.stats.get("day") != d:
            self.stats["day"] = d
            self.stats["sig_ep"] = 0
            self.stats["sig_ps"] = 0

    def add_signal(self, setup: str, ts_ms: int) -> None:
        self._roll_day(ts_ms)
        self.stats["sig_ep" if setup == "EP Long" else "sig_ps"] += 1

    def add_closed(self, total_r: float) -> None:
        self.stats["closed"] += 1
        self.stats["sum_r"] = float(self.stats["sum_r"]) + float(total_r)
        if total_r > 0:
            self.stats["wins"] += 1

    def signals_today(self, now_ms: int) -> Dict[str, int]:
        d = now_ms - now_ms % DAY_MS
        if self.stats.get("day") != d:
            return {"ep": 0, "ps": 0}
        return {"ep": int(self.stats["sig_ep"]), "ps": int(self.stats["sig_ps"])}
