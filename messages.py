"""Textos de Telegram (HTML). Todo lo dinámico se escapa con html.escape."""
from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import Config
from models import Event, Trade

_STOP_TXT = {
    "DAY": "mínimo/máximo del día",
    "OR": "mínimo/máximo del Opening Range",
    "ATR": "2×ATR14",
}


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def fp(x: Optional[float], prec: Optional[int] = None) -> str:
    """Precio con decimales adaptados (usa la precisión del contrato si se conoce)."""
    if x is None:
        return "—"
    if prec is not None:
        return f"{x:.{prec}f}"
    ax = abs(x)
    if ax == 0:
        return "0"
    dec = max(2, 5 - int(math.floor(math.log10(ax))))
    return f"{x:.{min(dec, 10)}f}"


def fq(x: float, prec: Optional[int]) -> str:
    if prec is not None:
        return f"{x:.{prec}f}"
    return f"{x:.6g}"


def sg(x: Optional[float], dec: int = 1, suffix: str = "%") -> str:
    """Número con signo (o — si no hay dato)."""
    return "—" if x is None else f"{x:+.{dec}f}{suffix}"


def num(x: Optional[float], dec: int = 1, suffix: str = "") -> str:
    return "—" if x is None else f"{x:.{dec}f}{suffix}"


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def fdt(ms: int, tzname: str = "UTC") -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    s = dt.strftime("%Y-%m-%d %H:%M UTC")
    if tzname and tzname.upper() != "UTC":
        tz = _tz(tzname)
        if tz is not None:
            loc = dt.astimezone(tz)
            s += f" ({loc.strftime('%H:%M')} {loc.tzname()})"
    return s


def fdur(ms: int) -> str:
    mins = max(0, int(ms // 60000))
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    if h < 48:
        return f"{h} h {m} min"
    return f"{h // 24} d {h % 24} h"


def tv_link(symbol: str) -> str:
    base = symbol.replace("-", "")
    return f'<a href="https://www.tradingview.com/chart/?symbol=BINGX%3A{esc(base)}.P">TradingView</a>'


def _bar_time(ev: Event, cfg: Config) -> str:
    """Hora de CIERRE de la vela que originó el evento."""
    return fdt(ev.ts + cfg.tf_ms, cfg.timezone)


# ═══════════════════════════ ENTRADAS ═══════════════════════════


def _trigger_text(d: Dict[str, Any], cfg: Config) -> str:
    pp = d.get("price_prec")
    t = d.get("trigger")
    if t == "ORH":
        extra = " · EP recién armado en esta vela" if d.get("just_armed") else ""
        return f"Ruptura del OR High ({fp(d.get('or_high'), pp)}){extra}"
    if t == "HH":
        return f"Ruptura del máximo de las últimas 10 velas ({fp(d.get('hh10'), pp)})"
    if t == "VWAP_EMA":
        return f"Recuperación de VWAP ({fp(d.get('vwap'), pp)}) y EMA21 ({fp(d.get('ema21'), pp)})"
    if t == "ORL":
        return f"Pérdida del OR Low ({fp(d.get('or_low'), pp)})"
    if t == "FIRST_RED":
        return "Primera vela roja tras vela verde"
    if t == "VWAP_FAIL":
        return f"Fallo/pérdida del VWAP ({fp(d.get('vwap'), pp)})"
    return str(t)


def fmt_entry(ev: Event, cfg: Config) -> str:
    d = ev.data
    is_long = d["side"] == "long"
    pp = d.get("price_prec")
    icon = "🟢" if is_long else "🔴"
    name = "EP LONG" if d["setup"] == "EP Long" else "PARABOLIC SHORT"
    cond = "&lt;" if is_long else "&gt;"

    L: List[str] = []
    L.append(f"{icon} <b>{name}</b> · <b>{esc(ev.symbol)}</b> · {esc(cfg.timeframe)}")
    L.append("")
    L.append("<b>Operación</b>")
    L.append(f"{'Compra' if is_long else 'Venta'} a mercado ≈ <code>{fp(d['entry'], pp)}</code> (cierre de la vela de señal)")
    L.append(
        f"🛑 Stop: <code>{fp(d['stop'], pp)}</code> · {d['stop_pct']:.2f}% del precio"
        f" ({num(d.get('stop_adr_mult'), 2)}× ADR)"
    )
    L.append(
        f"🎯 TP1 (+{cfg.partial_r:g}R): <code>{fp(d['tp1'], pp)}</code> ({d['tp1_pct']:.2f}%)"
        f" → cerrar {cfg.partial_pct:g}% y mover stop a BE"
    )
    if cfg.use_trail_ema:
        L.append(
            f"🔁 Resto: stop en BE y salir si un cierre diario {cond} EMA{cfg.trail_ema_len} diaria "
            f"(se evalúa al abrir cada día)"
        )
    else:
        L.append("🔁 Resto: stop en BE hasta que lo toque")
    if cfg.max_hold_bars > 0:
        L.append(f"⏱ Salida por tiempo: {cfg.max_hold_bars} velas")

    L.append("")
    L.append(f"<b>Tamaño sugerido</b> (equity ref. ${cfg.equity:,.0f} · riesgo {d['risk_pct']:.2f}%)")
    L.append(
        f"Cantidad: <code>{fq(d['qty'], d.get('qty_prec'))}</code> {esc(d.get('asset', ''))}"
        f" · Nocional ${d['notional']:,.2f} ({d['notional_pct']:.1f}% del equity)"
    )
    risk_line = f"Riesgo en $: ${d['risk_cash']:,.2f}"
    if d.get("capped"):
        risk_line += " · ⚠️ limitado por el tope de posición"
    L.append(risk_line)
    mq = d.get("min_qty")
    if mq is not None and d["qty"] < mq:
        L.append(f"⚠️ La cantidad es menor que el mínimo del contrato ({fq(mq, d.get('qty_prec'))})")

    L.append("")
    L.append("<b>Contexto</b>")
    if d["setup"] == "EP Long":
        L.append(f"Movimiento vs cierre de ayer: {sg(d.get('move'))} (gap {sg(d.get('gap'))})")
        L.append(f"RVOL a esta hora: {num(d.get('rvol'), 1, '×')} · ADR20: {num(d.get('adr'), 1, '%')}")
    else:
        L.append(
            f"Subida desde el mínimo de {cfg.ps_lookback}d: {sg(d.get('gain'), 0)}"
            f" · Días verdes seguidos: {d.get('green')}"
        )
        L.append(f"Extensión sobre EMA20 diaria: {sg(d.get('ext'), 0)} · ADR20: {num(d.get('adr'), 1, '%')}")
    L.append(f"Gatillo: {esc(_trigger_text(d, cfg))}")
    L.append(f"OR ({cfg.or_minutes}m): H {fp(d.get('or_high'), pp)} / L {fp(d.get('or_low'), pp)}")
    L.append(f"Día: H {fp(d.get('day_high'), pp)} / L {fp(d.get('day_low'), pp)} · ATR14 {fp(d.get('atr'), pp)}")
    L.append(f"Stop basado en: {_STOP_TXT.get(d.get('stop_mode'), '')} ± {cfg.sl_buf:g}×ATR")
    L.append(f"Operaciones hoy en este par: {d['trades_today']}/{d['max_trades']}")
    L.append("")
    L.append(f"🕒 Vela cerrada {_bar_time(ev, cfg)} · {tv_link(ev.symbol)}")
    return "\n".join(L)


# ═══════════════════════════ GESTIÓN ═══════════════════════════


def fmt_tp1(ev: Event, cfg: Config) -> str:
    d = ev.data
    pp = d.get("price_prec")
    icon = "🟢" if d["side"] == "long" else "🔴"
    return "\n".join([
        f"🎯 <b>TP1 alcanzado</b> · <b>{esc(ev.symbol)}</b> ({icon} {esc(d['setup'])})",
        f"Objetivo +{d['partial_r']:g}R: <code>{fp(d['tp1'], pp)}</code>",
        f"👉 Cerrar {d['partial_pct']:g}% de la posición y mover el stop del resto a BE: "
        f"<code>{fp(d['new_stop'], pp)}</code>",
        f"Resultado asegurado: {d['realized_r']:+.2f}R",
        "",
        f"🕒 Vela cerrada {_bar_time(ev, cfg)} · {tv_link(ev.symbol)}",
    ])


_EXIT_TITLES = {
    "stop": ("🛑", "STOP alcanzado"),
    "be": ("⚪", "Stop en BE alcanzado"),
    "trail": ("🔔", "Salida por trail EMA"),
    "maxbars": ("⏱", "Salida por tiempo"),
}


def fmt_exit(ev: Event, cfg: Config) -> str:
    d = ev.data
    pp = d.get("price_prec")
    icon, title = _EXIT_TITLES[ev.kind]
    side = "LONG" if d["side"] == "long" else "SHORT"
    L = [f"{icon} <b>{title}</b> · <b>{esc(ev.symbol)}</b> ({esc(d['setup'])} · {side})"]
    if ev.kind == "trail":
        cmp = "por debajo" if d["side"] == "long" else "por encima"
        L.append(
            f"El cierre diario de ayer ({fp(d.get('prior_close'), pp)}) quedó {cmp} de la EMA{d.get('ema_len')} "
            f"diaria ({fp(d.get('trail_ema'), pp)})."
        )
        L.append("👉 Cerrar el resto de la posición a mercado.")
    elif ev.kind == "maxbars":
        L.append(f"Se alcanzaron {d.get('bars')} velas en posición. 👉 Cerrar el resto a mercado.")
    elif ev.kind == "be":
        L.append("El precio volvió a la entrada tras el parcial.")
    L.append(f"Entrada <code>{fp(d['entry'], pp)}</code> → salida <code>{fp(d['exit'], pp)}</code>")
    pnl = d.get("pnl")
    res = f"Resultado total: <b>{d['total_r']:+.2f}R</b>"
    if pnl is not None:
        res += f" ({'+' if pnl >= 0 else '−'}${abs(pnl):,.2f} con el tamaño sugerido)"
    L.append(res)
    L.append(f"Duración: {fdur(d.get('held_ms', 0))}")
    L.append("")
    L.append(f"🕒 Vela cerrada {_bar_time(ev, cfg)} · {tv_link(ev.symbol)}")
    return "\n".join(L)


# ═══════════════════════════ AVISOS DE CONTEXTO ═══════════════════════════


def fmt_armed_ep(ev: Event, cfg: Config) -> str:
    d = ev.data
    if d.get("trigger") == "ORH":
        wait = f"ruptura del OR High ({fp(d.get('or_high'))})" if d.get("or_high") else "cierre del Opening Range"
    elif d.get("trigger") == "HH":
        wait = f"ruptura del máximo de 10 velas ({fp(d.get('hh10'))})"
    else:
        wait = "recuperación de VWAP + EMA21"
    return "\n".join([
        f"👀 <b>EP armado</b> · <b>{esc(ev.symbol)}</b> · {esc(cfg.timeframe)}",
        f"Movimiento vs cierre de ayer: {sg(d.get('move'))} · RVOL {num(d.get('rvol'), 1, '×')} · "
        f"ADR20 {num(d.get('adr'), 1, '%')}",
        f"Precio: <code>{fp(d.get('close'))}</code> · Esperando gatillo: {esc(wait)}",
        f"🕒 {_bar_time(ev, cfg)} · {tv_link(ev.symbol)}",
    ])


def fmt_armed_ps(ev: Event, cfg: Config) -> str:
    d = ev.data
    trig = {"ORL": f"pérdida del OR Low ({fp(d.get('or_low'))})",
            "FIRST_RED": "primera vela roja",
            "VWAP_FAIL": "fallo del VWAP"}.get(d.get("trigger"), str(d.get("trigger")))
    return "\n".join([
        f"👀 <b>Parabolic activo</b> · <b>{esc(ev.symbol)}</b> · {esc(cfg.timeframe)}",
        f"Subida {cfg.ps_lookback}d: {sg(d.get('gain'), 0)} · Días verdes: {d.get('green')} · "
        f"Extensión EMA20d: {sg(d.get('ext'), 0)}",
        f"Precio: <code>{fp(d.get('close'))}</code> · Esperando gatillo: {esc(trig)}",
        f"🕒 {_bar_time(ev, cfg)} · {tv_link(ev.symbol)}",
    ])


def format_event(ev: Event, cfg: Config) -> str:
    if ev.kind == "entry":
        return fmt_entry(ev, cfg)
    if ev.kind == "tp1":
        return fmt_tp1(ev, cfg)
    if ev.kind in _EXIT_TITLES:
        return fmt_exit(ev, cfg)
    if ev.kind == "armed_ep":
        return fmt_armed_ep(ev, cfg)
    if ev.kind == "armed_ps":
        return fmt_armed_ps(ev, cfg)
    return esc(f"{ev.kind} {ev.symbol}")


# ═══════════════════════════ ESTADO DEL BOT ═══════════════════════════


def _setups_txt(cfg: Config) -> str:
    return {"BOTH": "EP Long + Parabolic Short", "EP_LONG": "solo EP Long",
            "PARABOLIC_SHORT": "solo Parabolic Short"}[cfg.active_setup]


def fmt_start(cfg: Config, n_symbols: int, n_open: int, version: str) -> str:
    win = "todo el día" if cfg.window_min == 0 else f"{cfg.window_min / 60:g} h desde la apertura"
    return "\n".join([
        f"🤖 <b>Bot Qullamaggie iniciado</b> (v{esc(version)})",
        "Exchange: BingX · futuros perpetuos USDT-M",
        f"Símbolos analizados: <b>{n_symbols}</b>",
        f"Timeframe: {esc(cfg.timeframe)} · Setups: {_setups_txt(cfg)}",
        f"Riesgo: EP {cfg.risk_pct_ep:g}% · Short {cfg.risk_pct_short:g}% · equity ref. ${cfg.equity:,.0f}",
        f"Entradas: {win} · máx. {cfg.max_trades_day} operaciones/día por par",
        f"Posiciones virtuales abiertas recuperadas: {n_open}",
        "Solo envía señales: no opera por ti.",
    ])


def fmt_heartbeat(cfg: Config, now_ms: int, scan_no: int, last: Dict[str, Any], n_symbols: int,
                  sig_today: Dict[str, int], open_pos: List[Trade], stats: Dict[str, Any]) -> str:
    L = [
        f"💓 <b>Bot activo</b> · {fdt(now_ms, cfg.timezone)}",
        f"Ciclos: {scan_no} · último escaneo: {last.get('secs', 0):.0f}s · símbolos: {n_symbols}"
        f" (errores: {last.get('err', 0)})",
        f"Señales hoy: EP {sig_today['ep']} · Parabolic {sig_today['ps']}",
    ]
    if open_pos:
        L.append(f"Posiciones virtuales abiertas ({len(open_pos)}):")
        for t in open_pos[:15]:
            side = "LONG" if t.side == "long" else "SHORT"
            tp = "TP1 hecho, stop en BE" if t.partial_taken else f"TP1 pendiente {fp(t.tp1)}"
            L.append(f" • {esc(t.symbol)} {side} · entrada {fp(t.entry)} · stop {fp(t.stop)} · {tp}")
        if len(open_pos) > 15:
            L.append(f" … y {len(open_pos) - 15} más")
    else:
        L.append("Sin posiciones virtuales abiertas.")
    closed = int(stats.get("closed", 0))
    if closed:
        wr = stats.get("wins", 0) / closed * 100.0
        L.append(f"Acumulado virtual: {closed} cerradas · acierto {wr:.0f}% · ΣR {float(stats.get('sum_r', 0)):+.2f}")
    return "\n".join(L)
