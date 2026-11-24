"""Pruebas offline (no usan red ni Telegram).   Ejecuta:  python test_offline.py"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import random
import signal as _signal
import sys
import tempfile
import time
import traceback
from html.parser import HTMLParser
from typing import List, Tuple

from aiohttp import web

from bingx import BingXClient, BingXError, RateLimiter, parse_klines
from config import Config
from indicators import atr_series, ema, highest_prev, lowest_series, sma_series
import messages
from main import Engine, normalize_symbol
from notifier import Notifier
from models import DAY_MS, Candles, Event, SymMeta, SymState, Trade
from store import Store
from strategy import DAILY_SETTLE_MS, DailySeries, analyze

TF5 = 300_000
TODAY = 20_400 * DAY_MS  # un día UTC cualquiera (múltiplo exacto de 86400000 ms)
SYM = "TEST-USDT"
BASE_CFG = dataclasses.replace(Config(), dry_run=True)


# ───────────────────────── utilidades de datos sintéticos ─────────────────────────


def add(cs: Candles, ts: int, o: float, h: float, l: float, c: float, v: float) -> None:
    cs.ts.append(ts)
    cs.o.append(o)
    cs.h.append(h)
    cs.l.append(l)
    cs.c.append(c)
    cs.v.append(v)


def cut(cs: Candles, n: int) -> Candles:
    return Candles(cs.ts[:n], cs.o[:n], cs.h[:n], cs.l[:n], cs.c[:n], cs.v[:n])


def flat_day(base: float = 100.0, rng: float = 5.0, vol: float = 1e6) -> Tuple[float, ...]:
    """Día plano (open == close, no cuenta como verde) con rango = rng %."""
    return (base, base * (1 + rng / 200), base * (1 - rng / 200), base, vol)


def daily_candles(days: List[Tuple[float, ...]]) -> Candles:
    """Velas diarias de los días completos anteriores a TODAY + vela parcial de hoy."""
    cs = Candles()
    n = len(days)
    for k, (o, h, l, c, v) in enumerate(days):
        add(cs, TODAY - (n - k) * DAY_MS, o, h, l, c, v)
    last = days[-1][3]
    add(cs, TODAY, last, last, last, last, 0.0)
    return cs


def yesterday_flat(price: float, total_vol: float, tf_ms: int = TF5) -> Candles:
    cs = Candles()
    y0 = TODAY - DAY_MS
    nb = DAY_MS // tf_ms
    for k in range(nb):
        add(cs, y0 + k * tf_ms, price, price * 1.0015, price * 0.9985, price, total_vol / nb)
    return cs


def append_today(cs: Candles, bars: List[Tuple[float, ...]], tf_ms: int = TF5) -> None:
    for k, (o, h, l, c, v) in enumerate(bars):
        add(cs, TODAY + k * tf_ms, o, h, l, c, v)


def step(cfg: Config, ic_full: Candles, daily: Candles, st: SymState, k: int, meta=None,
         now_extra_ms: int = 6000) -> List[Event]:
    """Simula un ciclo del bot justo tras cerrar la vela k de hoy."""
    nb_yday = DAY_MS // cfg.tf_ms
    upto = cut(ic_full, nb_yday + k + 1)
    now = TODAY + (k + 1) * cfg.tf_ms + now_extra_ms
    ds = DailySeries(daily, cfg.ps_lookback, fetched_ms=now)
    evs, _ = analyze(SYM, upto, ds, cfg, st, now, meta)
    return evs


def kinds(evs: List[Event]) -> List[str]:
    return [e.kind for e in evs]


# ── escenario EP Long (con gap + ruptura del OR High con volumen) ──
def ep_case(tp_bars: bool = True, first_low: float = 105.8):
    daily = daily_candles([flat_day(100.0, 5.0, 1e6) for _ in range(40)])
    ic = yesterday_flat(100.0, 1e6)
    bars = [
        (106.0, 106.8, first_low, 106.5, 6944),    # k0  OR
        (106.5, 106.7, 106.0, 106.3, 6944),        # k1  OR
        (106.3, 106.6, 106.1, 106.5, 6944),        # k2  OR  (OR High = 106.8)
        (106.5, 108.5, 106.4, 108.2, 60000),       # k3  ruptura OR High + volumen -> ENTRADA
        (108.2, 110.0, 107.9, 109.5, 8000),        # k4
        (109.5, 113.0, 109.3, 112.8, 8000),        # k5  alcanza TP1
        (112.8, 113.2, 108.0, 110.0, 8000),        # k6  vuelve a la entrada -> stop BE
        (110.0, 110.5, 109.5, 110.0, 5000),        # k7
        (110.0, 110.5, 109.5, 110.0, 5000),        # k8
    ]
    if not tp_bars:
        bars = bars[:4] + [(108.2, 108.6, 105.0, 105.5, 8000)] + bars[7:]  # k4 pierde el stop
    append_today(ic, bars)
    return ic, daily


# ── escenario Parabolic Short ──
def ps_case(stop_variant: bool = False):
    days = [flat_day(100.0, 5.0, 1e6) for _ in range(21)]
    px = 100.0
    for nxt in (120.0, 145.0, 175.0, 210.0):  # 4 días verdes parabólicos
        days.append((px, nxt * 1.02, px * 0.99, nxt, 3e6))
        px = nxt
    daily = daily_candles(days)
    ic = yesterday_flat(210.0, 3e6)
    bars = [
        (236.0, 240.0, 235.0, 238.0, 3000),   # k0 OR
        (238.0, 240.5, 236.5, 237.0, 3000),   # k1 OR
        (237.0, 239.0, 234.0, 236.0, 3000),   # k2 OR (OR Low = 234)
        (236.0, 237.0, 229.0, 230.0, 3000),   # k3 pierde OR Low -> ENTRADA SHORT
        (230.0, 231.0, 222.0, 224.0, 3000),   # k4
        (224.0, 225.0, 209.0, 210.0, 3000),   # k5 TP1
        (210.0, 233.0, 209.0, 232.0, 3000),   # k6 vuelve a la entrada -> BE
        (232.0, 233.0, 231.0, 232.0, 3000),   # k7
    ]
    if stop_variant:
        bars[4] = (230.0, 242.0, 229.0, 241.0, 3000)  # k4 toca el stop
    append_today(ic, bars)
    return ic, daily


def ps_armed_case():
    """Parabolic cuyo contexto se activa a mitad de día (para probar el aviso 'Parabolic activo')."""
    ic, daily = ps_case()
    keep = DAY_MS // TF5
    ic = cut(ic, keep)
    append_today(ic, [
        (136.0, 137.0, 134.0, 135.0, 3000),   # k0: aún sin contexto (subida < 50%)
        (135.0, 161.0, 135.0, 160.0, 3000),   # k1: el contexto se activa
        (160.0, 161.0, 159.0, 160.0, 3000),   # k2
    ])
    return ic, daily


# ───────────────────────── pruebas de indicadores / parseo ─────────────────────────


def test_indicators():
    assert ema([1, 2, 3, 4, 5], 3) == [1, 1.5, 2.25, 3.125, 4.0625]
    assert sma_series([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    assert lowest_series([5, 3, 4, 1, 2], 3) == [None, None, 3, 1, 1]
    assert highest_prev([1, 5, 2, 3, 4], 2) == [None, None, 5, 5, 3]
    h, l, c = [10, 12, 13, 12], [9, 10, 11, 10], [9.5, 11, 12, 11]
    a = atr_series(h, l, c, 3)
    # TR: 1 (high-low), 2.5 (|12-9.5|), 2, 2 -> semilla SMA(3)=5.5/3 ; luego RMA
    assert a[:2] == [None, None] and abs(a[2] - 5.5 / 3) < 1e-12
    assert abs(a[3] - ((5.5 / 3) * 2 + 2) / 3) < 1e-12


def test_parse_klines_formats():
    rows_dict = [
        {"open": "2", "high": "3", "low": "1", "close": "2.5", "volume": "10", "time": 2000},
        {"open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "5", "time": 1000},
        {"open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "5", "time": 1000},  # duplicada
        {"open": "x", "high": "2", "low": "0.5", "close": "1.5", "volume": "5", "time": 3000},  # corrupta
    ]
    cs = parse_klines(rows_dict)
    assert cs.ts == [1000, 2000] and cs.c == [1.5, 2.5] and cs.v == [5.0, 10.0]
    cs2 = parse_klines([[1000, 1, 2, 0.5, 1.5, 5], [2000, 2, 3, 1, 2.5, 10]])
    assert cs2.ts == [1000, 2000] and cs2.h == [2.0, 3.0]
    assert len(parse_klines(None)) == 0
    cs3 = Candles([0, TF5], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1])
    assert len(cs3.closed_only(TF5 + 5, TF5)) == 1   # la 2ª vela aún está en formación
    assert len(cs3.closed_only(2 * TF5, TF5)) == 2


def test_normalize_symbol():
    assert normalize_symbol("btcusdt") == "BTC-USDT"
    assert normalize_symbol("eth/usdt") == "ETH-USDT"
    assert normalize_symbol("SOL-USDT") == "SOL-USDT"


def test_daily_series_matches_pine_semantics():
    days = [flat_day(100.0, 5.0, 1e6) for _ in range(30)]
    days.append((100.0, 111.0, 99.0, 110.0, 2e6))  # ayer: verde
    daily = daily_candles(days)
    now = TODAY + 10 * TF5
    ds = DailySeries(daily, 15, fetched_ms=now)
    ctx = ds.ctx_for_day(TODAY)
    assert ctx.prior_close == 110.0                 # cierre de ayer ([1])
    assert ctx.green_streak == 1
    assert ctx.base_low == 97.5                     # mínimo de los últimos 15 días
    exp_adr = (19 * 5.0 + (111 - 99) / 110 * 100) / 20
    assert abs(ctx.adr - exp_adr) < 1e-9
    assert abs(ctx.avg_vol - (19 * 1e6 + 2e6) / 20) < 1e-6
    # Nunca usa la vela diaria de hoy (en formación)
    assert ds.ctx_for_day(TODAY - DAY_MS).prior_close == 100.0
    # Si la descarga es anterior al cierre definitivo de ayer, no es fiable
    early = DailySeries(daily, 15, fetched_ms=TODAY + DAILY_SETTLE_MS - 1)
    assert early.ctx_for_day(TODAY) is None
    assert early.ctx_for_day(TODAY - DAY_MS) is not None


# ───────────────────────── pruebas de estrategia ─────────────────────────


def test_ep_flow_incremental():
    ic, daily = ep_case()
    st = SymState()
    got = {k: step(BASE_CFG, ic, daily, st, k) for k in range(0, 9)}
    seq = {k: kinds(v) for k, v in got.items()}
    assert seq == {0: [], 1: [], 2: [], 3: ["entry"], 4: [], 5: ["tp1"], 6: ["be"], 7: [], 8: []}, seq

    e = got[3][0].data
    assert e["setup"] == "EP Long" and e["side"] == "long" and e["trigger"] == "ORH"
    assert e["entry"] == 108.2
    assert e["stop"] < 105.8                       # low del día - colchón de ATR
    assert abs(e["tp1"] - (108.2 + e["stop_dist"] * 1.8)) < 1e-9
    assert e["stop_adr_mult"] < 1.3
    assert abs(e["risk_cash"] - 35.0) < 1e-6        # 0.35% de 10.000
    assert e["capped"] is False
    assert abs(e["move"] - 8.2) < 1e-9 and e["rvol"] > 1.5

    be = got[6][0].data
    assert abs(be["total_r"] - 0.9) < 1e-9          # 50% a +1.8R y el resto a BE
    assert abs(be["pnl"] - 0.9 * e["risk_cash"]) < 1e-6
    assert st.pos is None and st.trades_count == 1
    assert st.last_ts == TODAY + 8 * TF5


def test_ep_flow_stop_hit_and_reentry():
    ic, daily = ep_case(tp_bars=False)
    st = SymState()
    got = [step(BASE_CFG, ic, daily, st, k) for k in range(0, 6)]
    seq = [kinds(g) for g in got]
    # k4 toca el stop; en k5 el precio vuelve a cerrar sobre el OR High (cruce) y, como EP sigue armado
    # y quedan operaciones del día (máx. 2), la estrategia reentra: así se comporta el Pine.
    assert seq == [[], [], [], ["entry"], ["stop"], ["entry"]], seq
    assert got[4][0].data["total_r"] <= -1 + 1e-9 and not got[4][0].data["partial_taken"]
    assert st.trades_count == 2 and st.pos is not None

    # Con MAX_TRADES_DAY=1 no hay segunda entrada
    cfg1 = dataclasses.replace(BASE_CFG, max_trades_day=1)
    st1 = SymState()
    seq1 = [kinds(step(cfg1, ic, daily, st1, k)) for k in range(0, 6)]
    assert seq1 == [[], [], [], ["entry"], ["stop"], []], seq1
    assert st1.trades_count == 1 and st1.pos is None


def test_ps_flow_incremental():
    ic, daily = ps_case()
    st = SymState()
    got = {k: step(BASE_CFG, ic, daily, st, k) for k in range(0, 8)}
    seq = {k: kinds(v) for k, v in got.items()}
    assert seq == {0: [], 1: [], 2: [], 3: ["entry"], 4: [], 5: ["tp1"], 6: ["be"], 7: []}, seq
    e = got[3][0].data
    assert e["setup"] == "Parabolic Short" and e["side"] == "short" and e["trigger"] == "ORL"
    assert e["stop"] > 240.5 and e["tp1"] < e["entry"]
    assert e["gain"] > 50 and e["green"] == 4 and e["ext"] > 25
    assert e["rvol"] < 1.5                          # no hay EP: gana el short
    assert abs(got[6][0].data["total_r"] - 0.9) < 1e-9


def test_ps_flow_stop_hit():
    ic, daily = ps_case(stop_variant=True)
    st = SymState()
    seq = [kinds(step(BASE_CFG, ic, daily, st, k)) for k in range(0, 6)]
    assert seq == [[], [], [], ["entry"], ["stop"], []], seq
    ev = step(BASE_CFG, ic, daily, SymState(), 3)  # entrada (estado nuevo, solo última vela)
    assert kinds(ev) == ["entry"]


def test_active_setup_filter():
    ic, daily = ep_case()
    cfg = dataclasses.replace(BASE_CFG, active_setup="PARABOLIC_SHORT")
    st = SymState()
    assert all(step(cfg, ic, daily, st, k) == [] for k in range(0, 9))
    ic2, daily2 = ps_case()
    cfg2 = dataclasses.replace(BASE_CFG, active_setup="EP_LONG")
    st2 = SymState()
    assert all(step(cfg2, ic2, daily2, st2, k) == [] for k in range(0, 8))


def test_stop_too_wide_is_rejected_and_not_counted():
    ic, daily = ep_case(first_low=99.0)  # mínimo del día muy lejos -> stop > 1.3 ADR
    st = SymState()
    for k in range(0, 4):
        assert step(BASE_CFG, ic, daily, st, k) == []
    assert st.pos is None and st.trades_count == 0


def test_trail_exit_on_new_day():
    days = [flat_day(100.0, 5.0, 1e6) for _ in range(40)]
    days.append((100.0, 101.0, 89.0, 90.0, 1e6))  # ayer cierra por debajo de la EMA10 diaria
    daily = daily_candles(days)
    ic = yesterday_flat(90.0, 1e6)
    append_today(ic, [(90.5, 91.5, 90.2, 91.0, 3000), (91.0, 91.5, 90.5, 91.2, 3000)])
    y_last = TODAY - TF5
    trade = Trade(symbol=SYM, setup="EP Long", side="long", entry=80.0, init_stop=75.0, stop=80.0, risk=5.0,
                  tp1=89.0, qty=7.0, risk_cash=35.0, entry_ts=TODAY - DAY_MS + 10 * TF5,
                  entry_day=TODAY - DAY_MS, tf="5m", partial_taken=True, remaining=0.5, realized_r=0.9)
    st = SymState(last_ts=y_last, trades_day=TODAY - DAY_MS, trades_count=1, pos=trade)
    evs = step(BASE_CFG, ic, daily, st, 0)
    assert kinds(evs) == ["trail"], kinds(evs)
    d = evs[0].data
    assert d["prior_close"] == 90.0 and d["trail_ema"] > 98
    assert abs(d["total_r"] - (0.9 + 0.5 * (91.0 - 80.0) / 5.0)) < 1e-9
    assert st.pos is None

    # Sin trail activado, la posición sigue abierta
    trade2 = dataclasses.replace(trade)
    st2 = SymState(last_ts=y_last, trades_day=TODAY - DAY_MS, trades_count=1, pos=trade2)
    cfg2 = dataclasses.replace(BASE_CFG, use_trail_ema=False)
    assert step(cfg2, ic, daily, st2, 0) == [] and st2.pos is not None
    # El trail solo actúa tras el parcial
    trade3 = dataclasses.replace(trade, partial_taken=False, stop=75.0, remaining=1.0, realized_r=0.0, tp1=95.0)
    st3 = SymState(last_ts=y_last, trades_day=TODAY - DAY_MS, trades_count=1, pos=trade3)
    assert step(BASE_CFG, ic, daily, st3, 0) == [] and st3.pos is not None


def test_max_hold_bars_exit():
    ic, daily = ep_case()
    cfg = dataclasses.replace(BASE_CFG, max_hold_bars=2)
    st = SymState()
    got = [step(cfg, ic, daily, st, k) for k in range(0, 7)]
    seq = [kinds(g) for g in got]
    # Entrada en k3. En k5 (2 velas después) se toca TP1 y, a cierre de esa vela, salta la salida por tiempo.
    assert seq[3] == ["entry"] and seq[4] == [] and seq[5] == ["tp1", "maxbars"] and seq[6] == [], seq
    e = got[3][0].data
    exp = 0.9 + 0.5 * (112.8 - 108.2) / e["stop_dist"]
    assert abs(got[5][1].data["total_r"] - exp) < 1e-9
    assert st.pos is None


def test_stale_signal_ignored():
    ic, daily = ep_case()
    st = SymState(last_ts=TODAY + 2 * TF5)  # la vela k3 está pendiente
    upto = cut(ic, DAY_MS // TF5 + 4)
    now = TODAY + 4 * TF5 + 3_600_000       # 1 hora después
    ds = DailySeries(daily, 15, fetched_ms=now)
    evs, info = analyze(SYM, upto, ds, BASE_CFG, st, now)
    assert evs == [] and st.pos is None and st.trades_count == 0 and info["new_bars"] == 1


def test_first_contact_emits_signal_of_last_bar():
    ic, daily = ep_case()
    st = SymState()  # bot recién arrancado, sin estado
    assert kinds(step(BASE_CFG, ic, daily, st, 3)) == ["entry"]  # el contexto se reconstruye del día


def test_no_duplicates_on_rerun():
    ic, daily = ep_case()
    st = SymState()
    for k in range(0, 4):
        step(BASE_CFG, ic, daily, st, k)
    assert step(BASE_CFG, ic, daily, st, 3) == []


def test_no_new_signal_while_position_open():
    # Con la posición abierta no se emiten nuevas entradas aunque haya otro cruce del OR High
    ic, daily = ep_case()
    st = SymState()
    for k in range(0, 5):
        step(BASE_CFG, ic, daily, st, k)
    assert st.pos is not None and st.trades_count == 1


# ───────────────────────── mensajes / estado ─────────────────────────


class _TagCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: List[str] = []
        self.ok = True

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.ok = False


def _assert_html_ok(text: str):
    assert 0 < len(text) < 4096, len(text)
    p = _TagCheck()
    p.feed(text)
    assert p.ok and not p.stack, text


def test_messages_render_for_all_kinds():
    cfg = dataclasses.replace(BASE_CFG, notify_armed=True, timezone="Europe/Madrid")
    meta = SymMeta(symbol=SYM, asset="TEST", price_prec=2, qty_prec=3, min_qty=0.01)
    seen = set()
    for ic, daily in (ep_case(), ep_case(tp_bars=False), ps_case(), ps_case(True)):
        st = SymState()
        for k in range(0, 8):
            for ev in step(cfg, ic, daily, st, k, meta):
                seen.add(ev.kind)
                txt = messages.format_event(ev, cfg)
                _assert_html_ok(txt)
                assert "TEST-USDT" in txt
    assert {"entry", "tp1", "be", "stop"} <= seen, seen
    # avisos de contexto (gatillo distinto para que no haya entrada en la misma vela)
    cfg_v = dataclasses.replace(cfg, ep_trigger="VWAP_EMA")
    ic, daily = ep_case()
    st = SymState()
    for k in range(0, 4):
        for ev in step(cfg_v, ic, daily, st, k, meta):
            seen.add(ev.kind)
            _assert_html_ok(messages.format_event(ev, cfg_v))
    ic, daily = ps_armed_case()
    cfg_p = dataclasses.replace(cfg, ps_trigger="VWAP_FAIL")
    st = SymState()
    for k in range(0, 3):
        for ev in step(cfg_p, ic, daily, st, k, meta):
            seen.add(ev.kind)
            _assert_html_ok(messages.format_event(ev, cfg_p))
    assert {"armed_ep", "armed_ps"} <= seen, seen

    # Mensajes de salida con motivos no cubiertos en los escenarios
    ev = Event("trail", SYM, TODAY, {"setup": "EP Long", "side": "long", "entry": 80.0, "exit": 91.0,
                                      "total_r": 2.0, "pnl": 70.0, "held_ms": 90_000_000,
                                      "prior_close": 90.0, "trail_ema": 98.2, "ema_len": 10})
    _assert_html_ok(messages.format_event(ev, cfg))
    ev = Event("maxbars", SYM, TODAY, {"setup": "Parabolic Short", "side": "short", "entry": 80.0, "exit": 78.0,
                                        "total_r": 0.4, "pnl": 14.0, "held_ms": 3_600_000, "bars": 20})
    _assert_html_ok(messages.format_event(ev, cfg))
    # Texto con caracteres peligrosos para HTML en el símbolo
    ev = Event("armed_ps", "A&B<X>-USDT", TODAY, {"gain": 80.0, "green": 4, "ext": 40.0, "or_low": 1.0,
                                                  "close": 2.0, "trigger": "ORL"})
    txt = messages.format_event(ev, cfg)
    _assert_html_ok(txt)
    assert "A&amp;B&lt;X&gt;" in txt

    st = Store(tempfile.mkdtemp())
    _assert_html_ok(messages.fmt_start(cfg, 600, 0, "1.0.0"))
    _assert_html_ok(messages.fmt_heartbeat(cfg, TODAY, 10, {"secs": 70.0, "err": 1}, 600,
                                           {"ep": 1, "ps": 0}, [], st.stats))
    print("      tipos de evento renderizados:", ", ".join(sorted(seen)))


def test_armed_notifications_once_per_day_and_off_by_default():
    ic, daily = ps_armed_case()
    cfg = dataclasses.replace(BASE_CFG, notify_armed=True, ps_trigger="VWAP_FAIL")
    st = SymState()
    seq = [kinds(step(cfg, ic, daily, st, k)) for k in range(0, 3)]
    assert seq == [[], ["armed_ps"], []], seq
    # Desactivado por defecto
    st2 = SymState()
    assert all(step(dataclasses.replace(cfg, notify_armed=False), ic, daily, st2, k) == [] for k in range(0, 3))
    # EP armado sin que haya entrada en esa vela
    ic2, d2 = ep_case()
    cfg_e = dataclasses.replace(BASE_CFG, notify_armed=True, ep_trigger="VWAP_EMA")
    st3 = SymState()
    seq3 = [kinds(step(cfg_e, ic2, d2, st3, k)) for k in range(0, 4)]
    assert seq3[3] == ["armed_ep"], seq3
    # Con el gatillo ORH la entrada ocurre en la misma vela y el aviso se suprime
    cfg_o = dataclasses.replace(BASE_CFG, notify_armed=True, ep_trigger="ORH")
    st4 = SymState()
    seq4 = [kinds(step(cfg_o, ic2, d2, st4, k)) for k in range(0, 4)]
    assert seq4[3] == ["entry"], seq4


def test_entry_message_has_all_key_fields():
    cfg = dataclasses.replace(BASE_CFG)
    ic, daily = ep_case()
    st = SymState()
    entry = [e for k in range(0, 4) for e in step(cfg, ic, daily, st, k, SymMeta(SYM, "TEST", 2, 3, 0.01))][0]
    txt = messages.fmt_entry(entry, cfg)
    for needle in ("EP LONG", "TEST-USDT", "Stop", "TP1", "RVOL", "OR (15m)", "Gatillo", "Cantidad",
                   "Riesgo en $", "108.20", "TradingView", "BINGX%3ATESTUSDT.P"):
        assert needle in txt, (needle, txt)


def test_store_roundtrip_and_corruption():
    d = tempfile.mkdtemp()
    s = Store(d)
    st = s.sym("AAA-USDT")
    st.last_ts = 123
    st.pos = Trade(symbol="AAA-USDT", setup="EP Long", side="long", entry=1.0, init_stop=0.9, stop=0.9,
                   risk=0.1, tp1=1.18, qty=5.0, risk_cash=0.5, entry_ts=1, entry_day=0, tf="5m")
    s.add_signal("EP Long", TODAY)
    s.add_closed(0.9)
    s.add_closed(-1.0)
    s.save()
    s2 = Store(d)
    assert s2.sym("AAA-USDT").last_ts == 123 and s2.has_pos("AAA-USDT")
    assert s2.sym("AAA-USDT").pos.tp1 == 1.18
    assert s2.stats["closed"] == 2 and s2.stats["wins"] == 1 and abs(s2.stats["sum_r"] + 0.1) < 1e-9
    assert s2.signals_today(TODAY + 1000) == {"ep": 1, "ps": 0}
    assert s2.signals_today(TODAY + 2 * DAY_MS) == {"ep": 0, "ps": 0}
    s2.prune([])  # con posición abierta no se borra
    assert "AAA-USDT" in s2.symbols
    # fichero corrupto: no revienta y se aparta
    with open(os.path.join(d, "state.json"), "w") as f:
        f.write("{no es json")
    s3 = Store(d)
    assert s3.symbols == {} and any(n.startswith("state.json.corrupto") for n in os.listdir(d))


def test_tp1_and_exit_messages_use_contract_price_precision():
    cfg = dataclasses.replace(BASE_CFG)
    ic, daily = ep_case()
    st = SymState()
    msgs = [messages.format_event(e, cfg) for k in range(0, 8)
            for e in step(cfg, ic, daily, st, k, SymMeta(SYM, "TEST", 2, 3, 0.01))]
    tp1_txt = [m for m in msgs if "TP1 alcanzado" in m][0]
    assert "<code>112.67</code>" in tp1_txt and "<code>108.20</code>" in tp1_txt, tp1_txt
    be_txt = [m for m in msgs if "Stop en BE" in m][0]
    assert "<code>108.20</code>" in be_txt, be_txt


def test_env_example_matches_config_variables():
    here = os.path.dirname(os.path.abspath(__file__))
    import re

    src = open(os.path.join(here, "config.py"), encoding="utf-8").read()
    used = set(re.findall(r'_(?:str|float|int|bool|choice|csv)\(\s*"([A-Z_]+)"', src))
    doc = open(os.path.join(here, "env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", doc, flags=re.M))
    assert used, "no se encontraron variables en config.py"
    assert used <= documented, f"variables sin documentar en env.example: {sorted(used - documented)}"
    assert documented <= used, f"variables en env.example que config.py no lee: {sorted(documented - used)}"


def test_dotenv_loader_does_not_override_existing_env():
    from config import load_dotenv

    d = tempfile.mkdtemp()
    path = os.path.join(d, ".env")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# comentario\nQ_TEST_A=hola\nexport Q_TEST_B=\"con espacios\"\nQ_TEST_C=valor # nota\n"
                "Q_TEST_D='x=y'\n\nlinea sin igual\nQ_TEST_E=ya_definida\n")
    for k in ("Q_TEST_A", "Q_TEST_B", "Q_TEST_C", "Q_TEST_D"):
        os.environ.pop(k, None)
    os.environ["Q_TEST_E"] = "de_railway"
    try:
        assert load_dotenv(path) == 4
        assert os.environ["Q_TEST_A"] == "hola" and os.environ["Q_TEST_B"] == "con espacios"
        assert os.environ["Q_TEST_C"] == "valor" and os.environ["Q_TEST_D"] == "x=y"
        assert os.environ["Q_TEST_E"] == "de_railway"  # las variables del entorno tienen prioridad
        assert load_dotenv(os.path.join(d, "no_existe.env")) == 0
    finally:
        for k in ("Q_TEST_A", "Q_TEST_B", "Q_TEST_C", "Q_TEST_D", "Q_TEST_E"):
            os.environ.pop(k, None)


def test_config_from_env():
    from config import ConfigError, load_config

    keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DRY_RUN", "TIMEFRAME", "ACTIVE_SETUP", "STOP_MODE",
            "EP_TRIGGER", "PS_TRIGGER", "SYMBOLS", "PARTIAL_PCT"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        try:
            load_config()
            raise AssertionError("debería exigir token y chat id")
        except ConfigError:
            pass
        os.environ.update({"TELEGRAM_BOT_TOKEN": '"123:ABC"', "TELEGRAM_CHAT_ID": "1, -100200",
                           "ACTIVE_SETUP": "Parabolic Short", "STOP_MODE": "Low/High del día",
                           "EP_TRIGGER": "VWAP/EMA", "PS_TRIGGER": "First Red", "SYMBOLS": "btcusdt, ETH-USDT",
                           "PARTIAL_PCT": "95", "TIMEFRAME": "15M"})
        c = load_config()
        assert c.telegram_token == "123:ABC" and c.telegram_chat_ids == ("1", "-100200")
        assert c.active_setup == "PARABOLIC_SHORT" and c.stop_mode == "DAY"
        assert c.ep_trigger == "VWAP_EMA" and c.ps_trigger == "FIRST_RED"
        assert c.symbols_override == ("BTCUSDT", "ETH-USDT") and c.partial_pct == 90.0
        assert c.timeframe == "15m" and c.tf_ms == 900_000
        os.environ["TIMEFRAME"] = "7m"
        try:
            load_config()
            raise AssertionError("TIMEFRAME inválido debería fallar")
        except ConfigError:
            pass
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ───────────────────────── motor completo con BingX simulado ─────────────────────────


class FakeBX:
    def __init__(self, series):
        self.series = series  # símbolo -> (intradía, diario)
        self.now = 0
        self.requests = 0

    def now_ms(self):
        return self.now

    async def sync_time(self):
        pass

    async def contracts(self):
        rows = [{"symbol": s, "currency": "USDT", "status": 1, "asset": s.split("-")[0],
                 "pricePrecision": 2, "quantityPrecision": 3, "tradeMinQuantity": 0.01} for s in self.series]
        rows += [
            {"symbol": "NCSKTSLA2USD-USDT", "currency": "USDT", "status": 1},   # acción: se excluye
            {"symbol": "NCFXEUR2USD-USDT", "currency": "USDT", "status": 1},    # forex: se excluye
            {"symbol": "OFF-USDT", "currency": "USDT", "status": 0},            # no operativo
            {"symbol": "BTC-USDC", "currency": "USDC", "status": 1},            # no es USDT
        ]
        return rows

    async def klines(self, sym, interval, limit):
        self.requests += 1
        ic, dc = self.series[sym]
        src, _ = (dc, DAY_MS) if interval == "1d" else (ic, TF5)
        n = sum(1 for t in src.ts if t <= self.now)  # incluye la vela en formación, como BingX
        a = max(0, n - limit)
        return Candles(src.ts[a:n], src.o[a:n], src.h[a:n], src.l[a:n], src.c[a:n], src.v[a:n])


class FakeNotifier:
    def __init__(self):
        self.messages: List[str] = []

    def send(self, text):
        self.messages.append(text)


def _series():
    ep_ic, ep_d = ep_case()
    ps_ic, ps_d = ps_case()
    flat_ic = yesterday_flat(100.0, 1e6)
    append_today(flat_ic, [(100.0, 100.2, 99.8, 100.0, 3472)] * 9)
    flat_d = daily_candles([flat_day() for _ in range(40)])
    return {"EPAA-USDT": (ep_ic, ep_d), "PSAA-USDT": (ps_ic, ps_d), "FLAT-USDT": (flat_ic, flat_d)}


def test_engine_end_to_end():
    async def run():
        tmp = tempfile.mkdtemp()
        cfg = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0, concurrency=4)
        bx, notif = FakeBX(_series()), FakeNotifier()
        eng = Engine(cfg, bx, Store(tmp), notif)
        await eng.refresh_universe()
        assert set(eng.universe) == {"EPAA-USDT", "PSAA-USDT", "FLAT-USDT"}, eng.universe.keys()
        assert eng.excluded == 2
        for k in range(0, 9):
            bx.now = TODAY + (k + 1) * TF5 + 6000
            await eng.scan_once()
        joined = "\n".join(notif.messages)
        assert len(notif.messages) == 6, [m.splitlines()[0] for m in notif.messages]
        assert joined.count("EP LONG") == 1 and joined.count("PARABOLIC SHORT") == 1
        assert joined.count("TP1 alcanzado") == 2 and joined.count("Stop en BE alcanzado") == 2
        assert "FLAT-USDT" not in joined
        st = eng.store.stats
        assert st["closed"] == 2 and st["wins"] == 2 and abs(st["sum_r"] - 1.8) < 1e-9
        assert eng.store.signals_today(bx.now) == {"ep": 1, "ps": 1}
        # No se repite nada si se vuelve a escanear sin velas nuevas
        before = len(notif.messages)
        await eng.scan_once()
        assert len(notif.messages) == before
        # Primera cantidad con la precisión del contrato (3 decimales)
        assert "Cantidad: <code>14." in notif.messages[0] or "Cantidad: <code>2." in notif.messages[0]
        print(f"      peticiones simuladas a BingX: {bx.requests}")

    asyncio.run(run())


def test_engine_restart_mid_trade():
    async def run():
        tmp = tempfile.mkdtemp()
        cfg = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0, concurrency=4)
        bx, notif = FakeBX(_series()), FakeNotifier()
        eng = Engine(cfg, bx, Store(tmp), notif)
        await eng.refresh_universe()
        for k in range(0, 5):  # hasta k4: posiciones abiertas
            bx.now = TODAY + (k + 1) * TF5 + 6000
            await eng.scan_once()
        assert len(notif.messages) == 2 and len(eng.store.open_positions()) == 2
        # "Reinicio" del proceso: todo nuevo, solo persiste state.json
        eng2 = Engine(cfg, bx, Store(tmp), notif)
        await eng2.refresh_universe()
        assert len(eng2.store.open_positions()) == 2
        for k in range(5, 9):
            bx.now = TODAY + (k + 1) * TF5 + 6000
            await eng2.scan_once()
        joined = "\n".join(notif.messages)
        assert len(notif.messages) == 6, [m.splitlines()[0] for m in notif.messages]
        assert joined.count("TP1 alcanzado") == 2 and joined.count("Stop en BE alcanzado") == 2
        # Un tercer arranque en frío no repite nada
        eng3 = Engine(cfg, bx, Store(tmp), notif)
        await eng3.refresh_universe()
        await eng3.scan_once()
        assert len(notif.messages) == 6

    asyncio.run(run())


def test_engine_survives_cycle_errors_and_stops_cleanly():
    async def run():
        tmp = tempfile.mkdtemp()
        cfg = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0)
        bx, notif = FakeBX(_series()), FakeNotifier()
        eng = Engine(cfg, bx, Store(tmp), notif)
        calls = {"n": 0}
        orig = eng.scan_once

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("fallo simulado")
            if calls["n"] == 3:
                eng.stop.set()  # como un SIGTERM de Railway
            await orig()

        async def no_sleep():
            return None

        eng.scan_once = flaky
        eng._sleep_until_next_bar = no_sleep
        bx.now = TODAY + 4 * TF5 + 6000
        await asyncio.wait_for(eng.run(), timeout=30)
        assert calls["n"] == 3  # el error del 1.er ciclo no mató el bucle
        assert any("Error interno" in m for m in notif.messages)
        assert any("Bot Qullamaggie iniciado" in m for m in notif.messages)
        assert any("EP LONG" in m for m in notif.messages)  # y siguió generando señales

    asyncio.run(run())


def test_liquidity_filter_never_abandons_an_open_position():
    async def run():
        tmp = tempfile.mkdtemp()
        base = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0)
        bx, notif = FakeBX(_series()), FakeNotifier()
        eng = Engine(base, bx, Store(tmp), notif)
        await eng.refresh_universe()
        for k in range(0, 4):  # k3: entran EPAA y PSAA
            bx.now = TODAY + (k + 1) * TF5 + 6000
            await eng.scan_once()
        assert len(eng.store.open_positions()) == 2
        # Ahora un filtro de liquidez imposible: las posiciones abiertas se siguen igualmente
        strict = dataclasses.replace(base, min_daily_vol_usdt=1e15)
        eng2 = Engine(strict, bx, Store(tmp), notif)
        await eng2.refresh_universe()
        for k in range(4, 8):
            bx.now = TODAY + (k + 1) * TF5 + 6000
            await eng2.scan_once()
        joined = "\n".join(notif.messages)
        assert joined.count("TP1 alcanzado") == 2 and joined.count("Stop en BE alcanzado") == 2
        # y sin posición el filtro sí descarta el par (FLAT no genera ni una petición intradía)
        before = bx.requests
        eng3 = Engine(strict, bx, Store(tempfile.mkdtemp()), FakeNotifier())
        await eng3.refresh_universe()
        bx.now = TODAY + 9 * TF5 + 6000
        await eng3.scan_once()
        assert bx.requests - before == 3  # solo las 3 descargas diarias; ninguna intradía

    asyncio.run(run())


def test_error_alert_names_the_most_common_error():
    class FailingBX(FakeBX):
        async def klines(self, *a, **k):
            raise BingXError("HTTP 403 (¿bloqueo geográfico? prueba otra región en Railway)")

    async def run():
        flat_ic = yesterday_flat(100.0, 1e6)
        flat_d = daily_candles([flat_day() for _ in range(40)])
        series = {f"S{i}-USDT": (flat_ic, flat_d) for i in range(10)}
        tmp = tempfile.mkdtemp()
        cfg = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0)
        bx, notif = FailingBX(series), FakeNotifier()
        bx.now = TODAY + 5 * TF5 + 6000
        eng = Engine(cfg, bx, Store(tmp), notif)
        await eng.refresh_universe()
        await eng.scan_once()
        assert eng.last_scan["err"] == 10
        assert len(notif.messages) == 1 and "BingX responde mal" in notif.messages[0]
        assert "403" in notif.messages[0] and "×10" in notif.messages[0]
        _assert_html_ok(notif.messages[0])
        await eng.scan_once()  # la alerta no se repite en la misma hora
        assert len(notif.messages) == 1

    asyncio.run(run())


def test_engine_new_bot_without_state_only_looks_at_last_bar():
    async def run():
        tmp = tempfile.mkdtemp()
        cfg = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0)
        bx, notif = FakeBX(_series()), FakeNotifier()
        eng = Engine(cfg, bx, Store(tmp), notif)
        await eng.refresh_universe()
        bx.now = TODAY + 9 * TF5 + 6000  # arranca tarde: ya pasó todo
        await eng.scan_once()
        assert notif.messages == []  # no inunda con señales históricas

    asyncio.run(run())


# ───────────────────────── fuzz: incremental == por lotes ─────────────────────────


def fuzz_market(seed: int, tf_ms: int, n_old_days: int = 40, n_days: int = 3):
    rnd = random.Random(seed)
    bpd = DAY_MS // tf_ms
    old: List[Tuple[float, ...]] = []
    price = 100.0
    for _ in range(n_old_days):
        o = price
        c = o * (1 + rnd.gauss(0, 0.04))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.015)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.015)))
        old.append((o, h, l, c, bpd * 1000 * (0.6 + 0.8 * rnd.random())))
        price = c
    ic = Candles()
    daily = Candles()
    for k, (o, h, l, c, v) in enumerate(old):
        add(daily, TODAY - (n_old_days + n_days - k) * DAY_MS, o, h, l, c, v)
    for d in range(n_days):
        t0 = TODAY - (n_days - 1 - d) * DAY_MS
        bias = rnd.gauss(0, 0.0012)
        day_bars = []
        for b in range(bpd):
            o = price
            c = o * (1 + bias + rnd.gauss(0, 0.0016))
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0008)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.0008)))
            v = abs(rnd.gauss(1000, 400)) * (5 if rnd.random() < 0.05 else 1)
            add(ic, t0 + b * tf_ms, o, h, l, c, v)
            day_bars.append((o, h, l, c, v))
            price = c
        add(daily, t0, day_bars[0][0], max(x[1] for x in day_bars), min(x[2] for x in day_bars),
            day_bars[-1][3], sum(x[4] for x in day_bars))
    return ic, daily


FUZZ_CFG = dataclasses.replace(
    BASE_CFG, timeframe="15m", ep_min_change=0.3, ep_min_adr=0.1, ep_min_rvol=0.2, ep_trigger="HH",
    ps_min_gain=1.0, ps_min_green=1, ps_min_ext=0.3, ps_trigger="FIRST_RED", window_min=0,
    max_trades_day=3, max_adr_mult=3.0, partial_r=1.0, max_hold_bars=20, skip_first_min=0,
)


def _sig(ev: Event):
    d = ev.data
    return (ev.kind, ev.ts, round(d.get("entry", 0.0), 8), round(d.get("stop", 0.0), 8),
            round(d.get("total_r", 0.0), 8), d.get("setup"))


def _run_incremental(cfg, ic, daily, j0, j1):
    st = SymState(last_ts=ic.ts[j0 - 1])
    out: List[Event] = []
    for j in range(j0, j1 + 1):
        now = ic.ts[j] + cfg.tf_ms + 6000
        ds = DailySeries(daily, cfg.ps_lookback, fetched_ms=now)
        evs, _ = analyze(SYM, cut(ic, j + 1), ds, cfg, st, now)
        out += evs
    return out, st


def _run_batch(cfg, ic, daily, j0, j1):
    st = SymState(last_ts=ic.ts[j0 - 1])
    now = ic.ts[j1] + cfg.tf_ms + 6000
    ds = DailySeries(daily, cfg.ps_lookback, fetched_ms=now)
    evs, _ = analyze(SYM, cut(ic, j1 + 1), ds, dataclasses.replace(cfg, max_signal_age_bars=1e9), st, now)
    return evs, st


def _check_invariants(cfg, evs: List[Event]):
    open_ = False
    partial = False
    per_day = {}
    for ev in evs:
        if ev.kind == "entry":
            assert not open_, "entrada con posición abierta"
            open_, partial = True, False
            day = ev.ts - ev.ts % DAY_MS
            per_day[day] = per_day.get(day, 0) + 1
            assert per_day[day] <= cfg.max_trades_day
            assert ev.data["stop_dist"] > 0 and ev.data["qty"] > 0
        elif ev.kind == "tp1":
            assert open_ and not partial
            partial = True
        else:
            assert open_, f"salida {ev.kind} sin posición"
            open_ = False
            r = ev.data["total_r"]
            if ev.kind == "stop":
                assert not ev.data["partial_taken"] and r <= -1 + 1e-9, r
            if ev.kind == "be":
                assert ev.data["partial_taken"] and r <= 0.5 * cfg.partial_r + 1e-9, r


def test_fuzz_incremental_equals_batch():
    total = 0
    kinds_seen = set()
    for seed in range(14):
        ic, daily = fuzz_market(seed, FUZZ_CFG.tf_ms)
        bpd = DAY_MS // FUZZ_CFG.tf_ms
        j0, j1 = bpd + 5, 3 * bpd - 1
        a, st_a = _run_incremental(FUZZ_CFG, ic, daily, j0, j1)
        b, st_b = _run_batch(FUZZ_CFG, ic, daily, j0, j1)
        assert [_sig(e) for e in a] == [_sig(e) for e in b], f"seed {seed}: incremental != lotes"
        assert (st_a.pos is None) == (st_b.pos is None) and st_a.trades_count == st_b.trades_count
        _check_invariants(FUZZ_CFG, a)
        total += len(a)
        kinds_seen |= {e.kind for e in a}
        for e in a:  # los mensajes se generan sin errores con datos aleatorios
            _assert_html_ok(messages.format_event(e, FUZZ_CFG))
    assert total > 20, f"el fuzz apenas generó eventos ({total})"
    print(f"      fuzz: {total} eventos en 14 mercados aleatorios; tipos: {sorted(kinds_seen)}")


def test_fuzz_all_trigger_and_stop_combinations():
    combos = [
        ("DAY", "ORH", "ORL", 0, 0), ("OR", "VWAP_EMA", "VWAP_FAIL", 0, 0), ("ATR", "HH", "FIRST_RED", 0, 0),
        ("OR", "ORH", "FIRST_RED", 360, 720), ("ATR", "VWAP_EMA", "ORL", 360, 720), ("DAY", "HH", "VWAP_FAIL", 120, 0),
    ]
    total, per_setup = 0, {"EP Long": 0, "Parabolic Short": 0}
    for ci, (stop_mode, ept, pst, offset, window) in enumerate(combos):
        cfg = dataclasses.replace(FUZZ_CFG, stop_mode=stop_mode, ep_trigger=ept, ps_trigger=pst,
                                  sess_offset=offset, window_min=window)
        for seed in range(100 + 3 * ci, 103 + 3 * ci):
            ic, daily = fuzz_market(seed, cfg.tf_ms)
            bpd = DAY_MS // cfg.tf_ms
            j0, j1 = bpd + 5, 3 * bpd - 1
            a, _ = _run_incremental(cfg, ic, daily, j0, j1)
            b, _ = _run_batch(cfg, ic, daily, j0, j1)
            assert [_sig(e) for e in a] == [_sig(e) for e in b], (stop_mode, ept, pst, seed)
            _check_invariants(cfg, a)
            for e in a:
                _assert_html_ok(messages.format_event(e, cfg))
                if e.kind == "entry":
                    d = e.data
                    per_setup[d["setup"]] += 1
                    assert d["notional"] <= cfg.equity * cfg.max_pos_pct / 100 + 1e-6      # tope de posición
                    assert abs(d["risk_cash"] - d["qty"] * d["stop_dist"]) < 1e-6          # riesgo = qty x distancia
                    if d["side"] == "long":
                        assert d["stop"] < d["entry"] < d["tp1"]
                    else:
                        assert d["tp1"] < d["entry"] < d["stop"]
                    if window:  # ventana de entradas (min desde la apertura = 00:00 UTC + offset)
                        mins = (e.ts % DAY_MS) / 60000.0 - offset
                        assert cfg.skip_first_min <= mins <= window, mins
            total += len(a)
    assert total > 30 and all(v > 0 for v in per_setup.values()), (total, per_setup)
    print(f"      combinaciones: {total} eventos, entradas por setup: {per_setup}")


# ───────────────────────── desfase de la frontera diaria ─────────────────────────


def shift(cs: Candles, off: int) -> Candles:
    return Candles([t + off for t in cs.ts], list(cs.o), list(cs.h), list(cs.l), list(cs.c), list(cs.v))


OFF8H = 8 * 3_600_000


def test_day_offset_calendar_gives_same_signals():
    ic, daily = ep_case()
    ic_s, d_s = shift(ic, OFF8H), shift(daily, OFF8H)
    st = SymState()
    seq = []
    nb = DAY_MS // TF5
    for k in range(9):
        now = TODAY + OFF8H + (k + 1) * TF5 + 6000
        ds = DailySeries(d_s, 15, fetched_ms=now)
        evs, _ = analyze(SYM, cut(ic_s, nb + k + 1), ds, BASE_CFG, st, now, None, OFF8H)
        seq.append(kinds(evs))
    assert seq == [[], [], [], ["entry"], [], ["tp1"], ["be"], [], []], seq


def test_engine_autodetects_day_offset_and_guards_misalignment():
    async def run():
        series = {s: (shift(ic, OFF8H), shift(d, OFF8H)) for s, (ic, d) in _series().items()}
        tmp = tempfile.mkdtemp()
        cfg = dataclasses.replace(BASE_CFG, data_dir=tmp, scan_delay_sec=0.0)
        bx, notif = FakeBX(series), FakeNotifier()
        eng = Engine(cfg, bx, Store(tmp), notif)
        await eng.refresh_universe()
        bx.now = TODAY + OFF8H + TF5 + 6000  # hora "actual" simulada (la última vela diaria es la de hoy)
        await eng.detect_day_offset()
        assert eng.day_off == OFF8H
        for k in range(0, 9):
            bx.now = TODAY + OFF8H + (k + 1) * TF5 + 6000
            await eng.scan_once()
        assert len(notif.messages) == 6, [m.splitlines()[0] for m in notif.messages]

        # Si el desfase configurado no coincide con las velas diarias, se rechaza (no se opera con datos mal alineados)
        tmp2 = tempfile.mkdtemp()
        eng2 = Engine(dataclasses.replace(cfg, data_dir=tmp2, day_offset_min=0), bx, Store(tmp2), FakeNotifier())
        await eng2.refresh_universe()
        bx.now = TODAY + OFF8H + 4 * TF5 + 6000
        await eng2.scan_once()
        assert eng2.last_scan["err"] == 3 and eng2.notifier.messages == []

    asyncio.run(run())


# ───────────────────────── servidores HTTP simulados (BingX / Telegram) ─────────────────────────


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def test_bingx_client_against_mock_server():
    async def run():
        st = {"v3": 0, "v2": 0, "rate": 0, "r100410": 0, "bad": 0, "geo": 0, "calls": {}}
        rows = [{"open": "3", "high": "4", "low": "2", "close": "3.5", "volume": "7", "time": 3000},
                {"open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "5", "time": 1000},
                {"open": "2", "high": "3", "low": "1", "close": "2.5", "volume": "6", "time": 2000}]  # desordenadas

        async def server_time(req):
            return web.json_response({"code": 0, "msg": "", "data": {"serverTime": int(time.time() * 1000) + 1500}})

        async def contracts(req):
            return web.json_response({"code": 0, "msg": "", "data": [{"symbol": "BTC-USDT", "status": 1}]})

        async def v3(req):
            st["v3"] += 1
            return web.Response(status=404, text="not found")  # fuerza el fallback a v2

        async def v2(req):
            sym = req.query["symbol"]
            st["calls"][sym] = st["calls"].get(sym, 0) + 1
            if sym == "RATE-USDT" and st["calls"][sym] == 1:
                return web.Response(status=429, text="slow down")
            if sym == "CODE-USDT" and st["calls"][sym] == 1:
                return web.json_response({"code": 100410, "msg": "frequency limit"})
            if sym == "BAD-USDT":
                return web.json_response({"code": 100400, "msg": "invalid symbol"})
            if sym == "GEO-USDT":
                return web.Response(status=403, text="{}", content_type="application/json")
            if sym == "HTML-USDT":
                return web.Response(status=200, text="<html>oops</html>")
            assert req.query["interval"] == "5m" and req.query["limit"] == "100"
            return web.json_response({"code": 0, "msg": "", "data": rows})

        app = web.Application()
        app.router.add_get("/openApi/swap/v2/server/time", server_time)
        app.router.add_get("/openApi/swap/v2/quote/contracts", contracts)
        app.router.add_get("/openApi/swap/v3/quote/klines", v3)
        app.router.add_get("/openApi/swap/v2/quote/klines", v2)
        runner, base = await _serve(app)
        try:
            async with BingXClient(rate_per_sec=50, base_url=base) as bx:
                await bx.sync_time()
                assert 1000 < bx.time_offset_ms < 2000, bx.time_offset_ms
                assert abs(bx.now_ms() - int(time.time() * 1000) - 1500) < 500
                assert (await bx.contracts())[0]["symbol"] == "BTC-USDT"

                cs = await bx.klines("BTC-USDT", "5m", 100)          # v3 da 404 -> usa v2
                assert cs.ts == [1000, 2000, 3000] and cs.c == [1.5, 2.5, 3.5]
                assert st["v3"] == 1 and bx._kline_ok and bx._kline_idx == 1
                await bx.klines("BTC-USDT", "5m", 100)
                assert st["v3"] == 1                                  # recuerda la versión que funciona

                t0 = time.monotonic()
                cs = await bx.klines("RATE-USDT", "5m", 100)          # HTTP 429 una vez -> reintenta
                assert len(cs) == 3 and st["calls"]["RATE-USDT"] == 2 and time.monotonic() - t0 >= 1.9
                cs = await bx.klines("CODE-USDT", "5m", 100)          # código 100410 una vez -> reintenta
                assert len(cs) == 3 and st["calls"]["CODE-USDT"] == 2

                for sym, needle in (("BAD-USDT", "100400"), ("GEO-USDT", "403"), ("HTML-USDT", "no JSON")):
                    try:
                        await bx.klines(sym, "5m", 100)
                        raise AssertionError(f"{sym} debería fallar")
                    except BingXError as e:
                        assert needle in str(e), (sym, str(e))
                    assert st["calls"][sym] == 1, f"{sym}: no debe reintentarse ({st['calls'][sym]})"
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_rate_limiter_spacing():
    async def run():
        rl = RateLimiter(20.0)  # 1 petición cada 50 ms
        t0 = time.monotonic()
        await asyncio.gather(*(rl.acquire() for _ in range(7)))
        dt = time.monotonic() - t0
        assert 0.28 <= dt < 0.6, dt

    asyncio.run(run())


def test_telegram_notifier_against_mock_server():
    async def run():
        st = {"posts": [], "did429": False}

        async def send(req):
            body = await req.json()
            st["posts"].append(body)
            if body["chat_id"] == "429chat" and not st["did429"]:
                st["did429"] = True
                return web.json_response({"ok": False, "error_code": 429,
                                          "description": "Too Many Requests: retry after 1",
                                          "parameters": {"retry_after": 1}}, status=429)
            if body.get("parse_mode") and "<bad" in body["text"]:
                return web.json_response({"ok": False, "error_code": 400,
                                          "description": "Bad Request: can't parse entities: Unsupported start tag"},
                                         status=400)
            if body["chat_id"] == "forbidden":
                return web.json_response({"ok": False, "error_code": 403,
                                          "description": "Forbidden: bot was blocked by the user"}, status=403)
            return web.json_response({"ok": True, "result": {"message_id": 1}})

        app = web.Application()
        app.router.add_post("/botTKN/sendMessage", send)
        runner, base = await _serve(app)
        try:
            cfg = dataclasses.replace(BASE_CFG, dry_run=False, telegram_token="TKN",
                                      telegram_chat_ids=("111", "222"))
            n = Notifier(cfg)
            n.api_base = base
            await n.start()
            n.send("<b>Hola</b> señal")
            await n.close()
            assert [p["chat_id"] for p in st["posts"]] == ["111", "222"] and n.sent == 2 and n.failed == 0
            assert st["posts"][0]["parse_mode"] == "HTML" and st["posts"][0]["disable_web_page_preview"] is True

            # HTML inválido -> se reenvía como texto plano (no se pierde la señal)
            st["posts"].clear()
            n = Notifier(cfg)
            n.api_base = base
            await n.start()
            n.send("<bad>x</bad> &amp; y")
            await n.close()
            plain = [p for p in st["posts"] if p["chat_id"] == "111"]
            assert len(plain) == 2 and "parse_mode" in plain[0] and "parse_mode" not in plain[1]
            assert plain[1]["text"] == "x & y" and n.sent == 2

            # Límite de envío (429): espera retry_after y reintenta
            st["posts"].clear()
            n = Notifier(dataclasses.replace(cfg, telegram_chat_ids=("429chat",)))
            n.api_base = base
            await n.start()
            t0 = time.monotonic()
            n.send("mensaje")
            await n.close()
            assert n.sent == 1 and len(st["posts"]) == 2 and time.monotonic() - t0 >= 1.9

            # Error definitivo (403): no reintenta
            st["posts"].clear()
            n = Notifier(dataclasses.replace(cfg, telegram_chat_ids=("forbidden",)))
            n.api_base = base
            await n.start()
            n.send("mensaje")
            await n.close()
            assert n.sent == 0 and n.failed == 1 and len(st["posts"]) == 1
        finally:
            await runner.cleanup()

    asyncio.run(run())


# ───────────────────────── prueba de humo del programa real (python main.py) ─────────────────────────


def test_main_process_smoke_once_and_sigterm():
    """Arranca `python main.py` de verdad contra un BingX simulado: señales, state.json y cierre con SIGTERM."""

    async def run():
        here = os.path.dirname(os.path.abspath(__file__))
        tf = TF5
        now_ms = int(time.time() * 1000)
        if tf - (now_ms % tf) < 15_000:  # evitar el cambio de vela justo mientras arranca el proceso
            await asyncio.sleep((tf - now_ms % tf) / 1000.0 + 1.0)
            now_ms = int(time.time() * 1000)
        boundary = now_ms // tf * tf
        delta = boundary - (TODAY + 4 * tf)  # la vela k3 (la de la señal) acaba de cerrar
        series = {s: (shift(ic, delta), shift(d, delta)) for s, (ic, d) in _series().items()}

        async def server_time(req):
            return web.json_response({"code": 0, "data": {"serverTime": int(time.time() * 1000)}})

        async def contracts(req):
            rows = [{"symbol": s, "currency": "USDT", "status": 1, "asset": s.split("-")[0],
                     "pricePrecision": 2, "quantityPrecision": 3, "tradeMinQuantity": 0.01} for s in series]
            rows.append({"symbol": "NCSKTSLA2USD-USDT", "currency": "USDT", "status": 1})
            return web.json_response({"code": 0, "msg": "", "data": rows})

        async def klines(req):
            ic, dc = series[req.query["symbol"]]
            src = dc if req.query["interval"] == "1d" else ic
            now = int(time.time() * 1000)
            n = sum(1 for t in src.ts if t <= now)
            a = max(0, n - int(req.query["limit"]))
            rows = [{"open": str(src.o[i]), "high": str(src.h[i]), "low": str(src.l[i]), "close": str(src.c[i]),
                     "volume": str(src.v[i]), "time": src.ts[i]} for i in range(a, n)]
            rows.reverse()  # BingX puede devolverlas de más reciente a más antigua
            return web.json_response({"code": 0, "msg": "", "data": rows})

        app = web.Application()
        app.router.add_get("/openApi/swap/v2/server/time", server_time)
        app.router.add_get("/openApi/swap/v2/quote/contracts", contracts)
        app.router.add_get("/openApi/swap/v3/quote/klines", klines)
        runner, base = await _serve(app)
        try:
            tmp = tempfile.mkdtemp()
            env = dict(os.environ)
            env.update({"DRY_RUN": "true", "BINGX_BASE_URL": base, "DATA_DIR": tmp, "SCAN_DELAY_SEC": "0",
                        "HEARTBEAT_HOURS": "0", "RATE_PER_SEC": "50", "PYTHONUNBUFFERED": "1"})
            for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SYMBOLS", "TIMEFRAME"):
                env.pop(k, None)

            # 1) un solo escaneo
            p = await asyncio.create_subprocess_exec(
                sys.executable, "main.py", "--once", cwd=here, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(p.communicate(), timeout=60)
            text = out.decode()
            assert p.returncode == 0, text
            assert "EP LONG" in text and "PARABOLIC SHORT" in text and "EPAA-USDT" in text, text
            assert "FLAT-USDT" not in text.split("[DRY_RUN]", 1)[1] and "NCSK" not in text
            assert "Escaneo #1: 3 símbolos OK, 0 con error" in text, text
            assert "abren a las 00:00 UTC" in text or "se alinea con ellas" in text, text
            st = json_load(os.path.join(tmp, "state.json"))
            assert st["symbols"]["EPAA-USDT"]["pos"]["side"] == "long"
            assert st["symbols"]["PSAA-USDT"]["pos"]["side"] == "short"

            # 2) bucle continuo + SIGTERM (lo que hace Railway al redesplegar): cierre limpio
            tmp2 = tempfile.mkdtemp()
            env["DATA_DIR"] = tmp2
            p = await asyncio.create_subprocess_exec(
                sys.executable, "main.py", cwd=here, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            lines: List[str] = []
            while True:
                raw = await asyncio.wait_for(p.stdout.readline(), timeout=60)
                if not raw:
                    break
                lines.append(raw.decode())
                if "Escaneo #1" in lines[-1]:
                    break
            await asyncio.sleep(0.5)
            p.send_signal(_signal.SIGTERM)
            rest, _ = await asyncio.wait_for(p.communicate(), timeout=30)
            text2 = "".join(lines) + rest.decode()
            assert p.returncode == 0, text2
            assert "Bot detenido" in text2, text2
            assert os.path.exists(os.path.join(tmp2, "state.json"))
        finally:
            await runner.cleanup()

    asyncio.run(run())


def json_load(path: str):
    import json as _json

    with open(path, encoding="utf-8") as f:
        return _json.load(f)


# ───────────────────────── ejecución ─────────────────────────


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"OK    {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FALLA {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas correctas")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
