"""Qullamaggie Fused v2 (EP Long + Parabolic Short): port del script de TradingView.

La función `analyze` recorre las velas intradía en orden y replica, vela a vela, la
máquina de estados del Pine (día, Opening Range, EP armado, VWAP...). Solo se generan
señales para velas NUEVAS (posteriores a st.last_ts), de modo que cada señal se emite
una única vez aunque el escaneo se repita.

Orden de operaciones en cada vela (igual que el broker emulator de TradingView):
  1) Se aplican los stops/objetivos de la vela a la posición virtual abierta.
  2) Se evalúan señales de entrada (solo si no hay posición).
  3) Se evalúa la gestión "a cierre de vela": trail con EMA diaria y salida por tiempo.
"""
from __future__ import annotations

import bisect
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from indicators import atr_series, ema, highest_prev, lowest_series, sma_series
from models import Candles, DailyCtx, Event, SymMeta, SymState, Trade, day_start

log = logging.getLogger("strategy")

# Margen tras el cambio de día para considerar "definitiva" la vela diaria de ayer.
DAILY_SETTLE_MS = 45_000
EXIT_KINDS = ("stop", "be", "trail", "maxbars")


# ═══════════════════════════════════════════════════════════════
# DATOS DIARIOS CONFIRMADOS  (equivale a f_daily() con [1] + lookahead_on)
# ═══════════════════════════════════════════════════════════════


class DailySeries:
    """Métricas diarias calculadas sobre todas las velas diarias descargadas.

    ctx_for_day(D) devuelve los valores "a cierre del día D-1": nunca usa la vela diaria
    en formación, igual que el Pine (valores [1], sin repintado).
    """

    def __init__(self, dc: Candles, lookback: int, fetched_ms: int):
        n = len(dc)
        self.ts = list(dc.ts)
        self.close = list(dc.c)
        self.fetched_ms = fetched_ms
        rng = [((dc.h[i] - dc.l[i]) / dc.c[i] * 100.0) if dc.c[i] > 0 else 0.0 for i in range(n)]
        self.adr = sma_series(rng, 20)  # ta.sma((high-low)/close*100, 20)
        self.vavg = sma_series(dc.v, 20)  # ta.sma(volume, 20)
        self.e10 = ema(dc.c, 10)
        self.e20 = ema(dc.c, 20)
        self.low_n = lowest_series(dc.l, max(2, lookback))  # ta.lowest(low, lb)
        gs: List[int] = []
        s = 0
        for i in range(n):
            s = s + 1 if dc.c[i] > dc.o[i] else 0  # gs := close > open ? gs + 1 : 0
            gs.append(s)
        self.gs = gs

    def ctx_for_day(self, ds: int) -> Optional[DailyCtx]:
        # Si la descarga es anterior al cierre definitivo de ayer, no es fiable.
        if self.fetched_ms < ds + DAILY_SETTLE_MS:
            return None
        j = bisect.bisect_left(self.ts, ds) - 1  # última vela diaria con apertura < ds
        if j < 0:
            return None
        return DailyCtx(
            prior_close=self.close[j],
            adr=self.adr[j],
            avg_vol=self.vavg[j],
            ema10=self.e10[j],
            ema20=self.e20[j],
            green_streak=self.gs[j],
            base_low=self.low_n[j],
        )


# ═══════════════════════════════════════════════════════════════
# ANÁLISIS INTRADÍA
# ═══════════════════════════════════════════════════════════════


def analyze(
    symbol: str,
    ic: Candles,
    daily: DailySeries,
    cfg: Config,
    st: SymState,
    now_ms: int,
    meta: Optional[SymMeta] = None,
    day_off: int = 0,
) -> Tuple[List[Event], Dict[str, Any]]:
    """Procesa las velas CERRADAS nuevas de un símbolo y devuelve (eventos, info).

    day_off: desfase (ms) de la frontera diaria respecto a 00:00 UTC (0 salvo que BingX use otra).
    """
    meta = meta or SymMeta(symbol=symbol)
    events: List[Event] = []
    info: Dict[str, Any] = {"ep_armed": False, "ps_ctx": False, "rvol": None, "new_bars": 0}
    n = len(ic)
    if n == 0:
        return events, info

    tf_ms = cfg.tf_ms
    ep_on = cfg.active_setup in ("BOTH", "EP_LONG")
    ps_on = cfg.active_setup in ("BOTH", "PARABOLIC_SHORT")

    if st.last_ts <= 0:
        # Primer contacto con el símbolo: solo se evalúa la última vela cerrada.
        st.last_ts = ic.ts[-1] - tf_ms

    e21 = ema(ic.c, 21)
    atr = atr_series(ic.h, ic.l, ic.c, 14)
    hh10 = highest_prev(ic.h, 10)  # ta.highest(high, 10)[1]

    # Si la ventana descargada empieza a mitad de día, ese primer día es parcial.
    first_ds = day_start(ic.ts[0], day_off)
    first_partial = (ic.ts[0] - first_ds) > 2 * tf_ms

    ctx_by_day: Dict[int, Optional[DailyCtx]] = {}

    # Estado diario (var del Pine)
    prev_ds: Optional[int] = None
    day_open = day_high = day_low = 0.0
    day_vol = 0.0
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    ep_armed = False
    pv = vv = 0.0  # acumuladores de VWAP anclado al día
    prev_or_high: Optional[float] = None
    prev_or_low: Optional[float] = None
    prev_vwap: Optional[float] = None
    prev_ps_ctx = False
    rvol: Optional[float] = None
    ps_ctx = False

    for i in range(n):
        ts = ic.ts[i]
        o = ic.o[i]
        h = ic.h[i]
        l = ic.l[i]
        c = ic.c[i]
        v = ic.v[i]
        ds = day_start(ts, day_off)
        is_new_day = ds != prev_ds

        if is_new_day:
            day_open, day_high, day_low, day_vol = o, h, l, v
            or_high = None
            or_low = None
            ep_armed = False
            pv = vv = 0.0
        else:
            if h > day_high:
                day_high = h
            if l < day_low:
                day_low = l
            day_vol += v

        pv += (h + l + c) / 3.0 * v
        vv += v
        vwap = pv / vv if vv > 0 else None  # ta.vwap(hlc3), reinicia cada día

        mins_day = (ts - ds) / 60000.0
        mins_open = mins_day - cfg.sess_offset
        in_or = 0 <= mins_open < cfg.or_minutes
        or_done = mins_open >= cfg.or_minutes
        if in_or:
            or_high = h if or_high is None else max(or_high, h)
            or_low = l if or_low is None else min(or_low, l)

        if ds not in ctx_by_day:
            ctx_by_day[ds] = daily.ctx_for_day(ds)
        ctx = ctx_by_day[ds]
        pc = ctx.prior_close if ctx is not None else None
        adr = ctx.adr if ctx is not None else None
        avg_vol = ctx.avg_vol if ctx is not None else None

        # RVOL a esta hora (usa la hora de APERTURA de la vela, como el Pine)
        frac = min(max(mins_day / cfg.day_len, 0.05), 1.0)
        rvol = day_vol / (avg_vol * frac) if (avg_vol is not None and avg_vol > 0) else None

        in_window = mins_open >= cfg.skip_first_min and (cfg.window_min == 0 or mins_open <= cfg.window_min)

        # ── cruces y patrones de vela ──────────────────────────
        if i > 0:
            c1 = ic.c[i - 1]
            x_orh = or_high is not None and prev_or_high is not None and c > or_high and c1 <= prev_or_high
            x_orl = or_low is not None and prev_or_low is not None and c < or_low and c1 >= prev_or_low
            hh_now, hh_prev = hh10[i], hh10[i - 1]
            x_hh = hh_now is not None and hh_prev is not None and c > hh_now and c1 <= hh_prev
            vwap_up = (
                vwap is not None
                and prev_vwap is not None
                and c > e21[i]
                and c > vwap
                and c1 <= max(e21[i - 1], prev_vwap)
            )
            first_red = c < o and c1 >= ic.o[i - 1]
            vwap_fail = vwap is not None and c < vwap and c1 >= vwap and h >= vwap
        else:
            x_orh = x_orl = x_hh = vwap_up = first_red = vwap_fail = False

        # ── EP Long: contexto + gatillo ────────────────────────
        if pc is not None and pc > 0:
            chg = (c - pc) / pc * 100.0
            gap = (day_open - pc) / pc * 100.0
        else:
            chg = gap = None
        move = max(chg if chg is not None else 0.0, gap if gap is not None else 0.0)

        ep_context = (
            move >= cfg.ep_min_change
            and adr is not None
            and adr >= cfg.ep_min_adr
            and rvol is not None
            and rvol >= cfg.ep_min_rvol
        )
        ep_just_armed = ep_context and not ep_armed
        if ep_context:
            ep_armed = True

        trig_orh = (
            cfg.ep_trigger == "ORH"
            and or_done
            and or_high is not None
            and (x_orh or (ep_just_armed and c > or_high))
        )
        trig_hh = cfg.ep_trigger == "HH" and x_hh
        trig_vw = cfg.ep_trigger == "VWAP_EMA" and vwap_up
        ep_hit = trig_orh or trig_hh or trig_vw

        # ── Parabolic Short: contexto + gatillo ────────────────
        base_low = ctx.base_low if ctx is not None else None
        ema20d = ctx.ema20 if ctx is not None else None
        green = ctx.green_streak if ctx is not None else None
        gain = (c - base_low) / base_low * 100.0 if (base_low is not None and base_low > 0) else None
        ext = (c - ema20d) / ema20d * 100.0 if (ema20d is not None and ema20d > 0) else None
        ps_ctx = (
            gain is not None
            and gain >= cfg.ps_min_gain
            and green is not None
            and green >= cfg.ps_min_green
            and ext is not None
            and ext >= cfg.ps_min_ext
        )
        trig_orl = cfg.ps_trigger == "ORL" and or_done and x_orl
        trig_red = cfg.ps_trigger == "FIRST_RED" and first_red
        trig_pvw = cfg.ps_trigger == "VWAP_FAIL" and vwap_fail
        ps_hit = trig_orl or trig_red or trig_pvw

        # ── procesamiento de velas nuevas ──────────────────────
        if ts > st.last_ts:
            info["new_bars"] += 1
            if st.trades_day != ds:  # tradesToday := 0 al cambiar de día
                st.trades_day = ds
                st.trades_count = 0
            fresh = (now_ms - (ts + tf_ms)) / tf_ms <= cfg.max_signal_age_bars

            # 1) stops/objetivos de esta vela sobre la posición abierta
            if st.pos is not None and ts > st.pos.entry_ts:
                events.extend(_manage_bar(st, ts, o, h, l, c, cfg))

            # 2) señales de entrada
            can_trade = (
                st.pos is None
                and st.trades_count < cfg.max_trades_day
                and in_window
                and not (ds == first_ds and first_partial)
            )
            ep_signal = can_trade and ep_on and ep_armed and ep_hit
            ps_signal = can_trade and ps_on and ps_ctx and ps_hit and not ep_signal

            entered = False
            if ep_signal or ps_signal:
                if fresh:
                    common = {
                        "bar_ts": ts,
                        "or_high": or_high,
                        "or_low": or_low,
                        "day_high": day_high,
                        "day_low": day_low,
                        "hh10": hh10[i],
                        "vwap": vwap,
                        "ema21": e21[i],
                        "atr": atr[i],
                        "chg": chg,
                        "gap": gap,
                        "move": move,
                        "rvol": rvol,
                        "adr": adr,
                        "gain": gain,
                        "green": green,
                        "ext": ext,
                        "base_low": base_low,
                        "ema20d": ema20d,
                    }
                    if ep_signal:
                        key = "ORH" if trig_orh else ("HH" if trig_hh else "VWAP_EMA")
                        common["trigger"] = key
                        common["just_armed"] = ep_just_armed
                        ev = _make_entry(cfg, st, meta, symbol, "EP Long", True, ts, ds, c, atr[i], adr,
                                         day_low, or_low, common)
                    else:
                        key = "ORL" if trig_orl else ("FIRST_RED" if trig_red else "VWAP_FAIL")
                        common["trigger"] = key
                        ev = _make_entry(cfg, st, meta, symbol, "Parabolic Short", False, ts, ds, c, atr[i], adr,
                                         day_high, or_high, common)
                    if ev is not None:
                        events.append(ev)
                        entered = True
                else:
                    log.debug("%s: señal antigua ignorada (vela %s)", symbol, ts)

            # 3) gestión a cierre de vela: trail diario y salida por tiempo
            if st.pos is not None and not entered:
                t = st.pos
                if t.partial_taken and cfg.use_trail_ema and is_new_day and ctx is not None:
                    te = ctx.ema10 if cfg.trail_ema_len == 10 else ctx.ema20
                    if te is not None and ctx.prior_close is not None:
                        bad = ctx.prior_close < te if t.side == "long" else ctx.prior_close > te
                        if bad:
                            events.append(_close_trade(
                                st, c, ts, "trail",
                                {"trail_ema": te, "prior_close": ctx.prior_close, "ema_len": cfg.trail_ema_len},
                            ))
                if st.pos is not None and cfg.max_hold_bars > 0 and (ts - st.pos.entry_ts) // tf_ms >= cfg.max_hold_bars:
                    events.append(_close_trade(st, c, ts, "maxbars", {"bars": cfg.max_hold_bars}))

            # Avisos opcionales de contexto (no son señales de entrada)
            if cfg.notify_armed and fresh and not entered:
                if ep_on and ep_just_armed and st.armed_ep_day != ds:
                    st.armed_ep_day = ds
                    events.append(Event("armed_ep", symbol, ts, {
                        "move": move, "gap": gap, "rvol": rvol, "adr": adr, "or_high": or_high,
                        "or_done": or_done, "hh10": hh10[i], "close": c, "trigger": cfg.ep_trigger,
                    }))
                if ps_on and ps_ctx and not prev_ps_ctx and st.armed_ps_day != ds:
                    st.armed_ps_day = ds
                    events.append(Event("armed_ps", symbol, ts, {
                        "gain": gain, "green": green, "ext": ext, "or_low": or_low,
                        "or_done": or_done, "close": c, "trigger": cfg.ps_trigger,
                    }))

            st.last_ts = ts

        prev_ds = ds
        prev_or_high = or_high
        prev_or_low = or_low
        prev_vwap = vwap
        prev_ps_ctx = ps_ctx

    info["ep_armed"] = ep_armed
    info["ps_ctx"] = ps_ctx
    info["rvol"] = rvol
    return events, info


# ═══════════════════════════════════════════════════════════════
# ENTRADA + DIMENSIONAMIENTO  (bloques `if epSignal` / `if psSignal` del Pine)
# ═══════════════════════════════════════════════════════════════


def _floor_to(x: float, prec: Optional[int]) -> float:
    if prec is None:
        return x
    f = 10 ** prec
    return math.floor(x * f + 1e-9) / f


def _make_entry(
    cfg: Config,
    st: SymState,
    meta: SymMeta,
    symbol: str,
    setup: str,
    is_long: bool,
    ts: int,
    ds: int,
    c: float,
    atr_v: Optional[float],
    adr: Optional[float],
    ref_day: float,
    ref_or: Optional[float],
    ctxd: Dict[str, Any],
) -> Optional[Event]:
    if atr_v is None or adr is None:
        return None  # en Pine, stopOk sería falso con valores na

    mode = cfg.stop_mode
    if is_long:
        ref = ref_day if mode == "DAY" else (ref_or if mode == "OR" else c - 2.0 * atr_v)
        if ref is None:
            return None
        stop = ref - cfg.sl_buf * atr_v
        dist = c - stop
        risk_pct = cfg.risk_pct_ep
        mult = cfg.max_adr_mult
    else:
        ref = ref_day if mode == "DAY" else (ref_or if mode == "OR" else c + 2.0 * atr_v)
        if ref is None:
            return None
        stop = ref + cfg.sl_buf * atr_v
        dist = stop - c
        risk_pct = cfg.risk_pct_short
        mult = cfg.max_adr_mult * 1.2  # el Pine permite un stop 20% más ancho en el short

    adr_dist = adr / 100.0 * c
    if not (dist > 0 and dist <= adr_dist * mult):
        return None  # stopOk == false: se descarta la operación

    risk_cash = cfg.equity * risk_pct / 100.0
    qty_raw = risk_cash / dist
    max_qty = cfg.equity * cfg.max_pos_pct / 100.0 / c
    capped = qty_raw > max_qty
    qty_raw = min(qty_raw, max_qty)
    if qty_raw <= 0:
        return None
    qty = _floor_to(qty_raw, meta.qty_prec)
    qty_eff = qty if qty > 0 else qty_raw

    tp1 = c + dist * cfg.partial_r if is_long else c - dist * cfg.partial_r
    side = "long" if is_long else "short"

    st.trades_count += 1
    st.pos = Trade(
        symbol=symbol, setup=setup, side=side, entry=c, init_stop=stop, stop=stop, risk=dist, tp1=tp1,
        qty=qty_eff, risk_cash=qty_eff * dist, entry_ts=ts, entry_day=ds, tf=cfg.timeframe,
        price_prec=meta.price_prec,
    )

    data = dict(ctxd)
    data.update({
        "setup": setup,
        "side": side,
        "entry": c,
        "stop": stop,
        "stop_dist": dist,
        "stop_pct": dist / c * 100.0,
        "stop_adr_mult": dist / adr_dist if adr_dist > 0 else None,
        "tp1": tp1,
        "tp1_pct": abs(tp1 - c) / c * 100.0,
        "qty": qty_eff,
        "qty_prec": meta.qty_prec,
        "price_prec": meta.price_prec,
        "min_qty": meta.min_qty,
        "asset": meta.asset or symbol.split("-")[0],
        "notional": qty_eff * c,
        "notional_pct": qty_eff * c / cfg.equity * 100.0,
        "risk_cash": qty_eff * dist,
        "risk_pct": risk_pct,
        "capped": capped,
        "trades_today": st.trades_count,
        "max_trades": cfg.max_trades_day,
        "stop_mode": mode,
    })
    return Event("entry", symbol, ts, data)


# ═══════════════════════════════════════════════════════════════
# GESTIÓN DE LA POSICIÓN VIRTUAL
# ═══════════════════════════════════════════════════════════════


def _manage_bar(st: SymState, ts: int, o: float, h: float, l: float, c: float, cfg: Config) -> List[Event]:
    """Aplica una vela a la posición abierta: stop, parcial a +R y stop a BE.

    Si en la misma vela se tocan stop y objetivo se asume el stop primero (criterio prudente).
    El stop en BE rige desde la vela SIGUIENTE al parcial (igual que el Pine).
    """
    t = st.pos
    assert t is not None
    is_long = t.side == "long"
    evs: List[Event] = []

    stop_hit = l <= t.stop if is_long else h >= t.stop
    if stop_hit:
        px = min(t.stop, o) if is_long else max(t.stop, o)  # gap más allá del stop: se llena en la apertura
        evs.append(_close_trade(st, px, ts, "be" if t.partial_taken else "stop", {}))
        return evs

    if not t.partial_taken:
        tp_hit = h >= t.tp1 if is_long else l <= t.tp1
        if tp_hit:
            frac = cfg.partial_pct / 100.0
            t.partial_taken = True
            t.realized_r += frac * cfg.partial_r
            t.remaining = 1.0 - frac
            t.stop = t.entry  # stop a break-even para el resto
            evs.append(Event("tp1", t.symbol, ts, {
                "setup": t.setup,
                "side": t.side,
                "entry": t.entry,
                "tp1": t.tp1,
                "partial_pct": cfg.partial_pct,
                "partial_r": cfg.partial_r,
                "new_stop": t.stop,
                "price_prec": t.price_prec,
                "realized_r": t.realized_r,
                "remaining": t.remaining,
                "qty": t.qty,
                "risk_cash": t.risk_cash,
            }))
    return evs


def _close_trade(st: SymState, px: float, ts: int, kind: str, extra: Dict[str, Any]) -> Event:
    t = st.pos
    assert t is not None
    is_long = t.side == "long"
    r_exit = ((px - t.entry) if is_long else (t.entry - px)) / t.risk if t.risk > 0 else 0.0
    total_r = t.realized_r + t.remaining * r_exit
    data: Dict[str, Any] = {
        "setup": t.setup,
        "side": t.side,
        "entry": t.entry,
        "exit": px,
        "r_exit": r_exit,
        "total_r": total_r,
        "pnl": total_r * t.risk_cash,
        "partial_taken": t.partial_taken,
        "init_stop": t.init_stop,
        "tp1": t.tp1,
        "price_prec": t.price_prec,
        "entry_ts": t.entry_ts,
        "held_ms": ts - t.entry_ts,
        "qty": t.qty,
        "risk_cash": t.risk_cash,
    }
    data.update(extra)
    st.pos = None
    return Event(kind, t.symbol, ts, data)
