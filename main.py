"""Qullamaggie Fused v2 -> bot para BingX + Telegram.

Escanea TODOS los futuros perpetuos USDT-M de BingX al cierre de cada vela, aplica la
estrategia (EP Long + Parabolic Short) y envía las señales detalladas a Telegram.
Solo emite señales: no coloca órdenes.

Uso:   python main.py          (bucle continuo, el modo de Railway)
       python main.py --once   (un solo escaneo; útil para probar con DRY_RUN=true)
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from bingx import BingXClient
from config import Config, ConfigError, load_config
from messages import esc, fmt_heartbeat, fmt_start, format_event
from models import DAY_MS, Event, SymMeta, day_start
from notifier import Notifier
from store import Store
from strategy import DAILY_SETTLE_MS, EXIT_KINDS, DailySeries, analyze

__version__ = "1.0.0"

log = logging.getLogger("bot")

DAILY_BARS = 150  # velas diarias a descargar (ADR/EMA20 convergen de sobra)
MIN_INTRADAY_BARS = 30  # menos velas = moneda recién listada: se omite


def _to_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize_symbol(s: str) -> str:
    s = s.strip().upper().replace("/", "-").replace("_", "-")
    if "-" not in s and s.endswith("USDT"):
        s = s[:-4] + "-USDT"
    return s


class Engine:
    def __init__(self, cfg: Config, bx: Any, store: Store, notifier: Any):
        self.cfg = cfg
        self.bx = bx
        self.store = store
        self.notifier = notifier
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.stop = asyncio.Event()
        self.universe: Dict[str, SymMeta] = {}
        self.universe_ts = 0.0
        self.excluded = 0
        self.daily: Dict[str, DailySeries] = {}
        self.hot: Set[str] = set()  # símbolos con EP armado / Parabolic activo (se escanean primero)
        self.scan_no = 0
        self.last_scan: Dict[str, Any] = {"secs": 0.0, "ok": 0, "err": 0, "events": 0}
        self.last_heartbeat = time.time()
        self._last_sync = time.time()
        self._last_alert = 0.0
        self.day_off = max(0, cfg.day_offset_min) * 60_000  # frontera diaria (ms desde 00:00 UTC)

    # ── universo de símbolos ───────────────────────────────────

    async def refresh_universe(self) -> None:
        rows = await self.bx.contracts()
        rx = re.compile(self.cfg.exclude_regex) if self.cfg.exclude_regex else None
        only = {normalize_symbol(s) for s in self.cfg.symbols_override}
        uni: Dict[str, SymMeta] = {}
        excluded = 0
        for c in rows:
            sym = str(c.get("symbol", "")).upper()
            if not sym.endswith("-USDT"):
                continue
            if str(c.get("currency", "USDT")).upper() != "USDT":
                continue
            status = _to_int(c.get("status"))
            if status is not None and status != 1:  # 1 = operativo
                continue
            if rx is not None and rx.search(sym):
                excluded += 1
                continue
            if only and sym not in only:
                continue
            uni[sym] = SymMeta(
                symbol=sym,
                asset=str(c.get("asset") or sym.split("-")[0]),
                price_prec=_to_int(c.get("pricePrecision")),
                qty_prec=_to_int(c.get("quantityPrecision")),
                min_qty=_to_float(c.get("tradeMinQuantity")),
            )
        if not uni:
            raise RuntimeError("el filtro de símbolos dejó el universo vacío")
        for t in self.store.open_positions():  # seguir posiciones aunque el par ya no figure
            uni.setdefault(t.symbol, SymMeta(symbol=t.symbol))
        self.universe = uni
        self.excluded = excluded
        self.universe_ts = time.time()
        self.store.prune(uni.keys())
        for s in list(self.daily):
            if s not in uni:
                del self.daily[s]
                self.hot.discard(s)

    # ── procesamiento de un símbolo ────────────────────────────

    async def _process(self, sym: str, now_ms: int) -> int:
        if self.stop.is_set():
            return 0
        async with self.sem:
            if self.stop.is_set():
                return 0
            cfg = self.cfg
            tf_ms = cfg.tf_ms
            meta = self.universe.get(sym) or SymMeta(symbol=sym)

            # Apertura de la última vela cerrada y día al que pertenece
            last_closed_open = (now_ms // tf_ms - 1) * tf_ms
            need_day = day_start(last_closed_open, self.day_off)

            # Datos diarios: se descargan una vez por día (cuando ya es definitiva la vela de ayer)
            dser = self.daily.get(sym)
            if dser is None or dser.fetched_ms < need_day + DAILY_SETTLE_MS:
                dc = await self.bx.klines(sym, "1d", DAILY_BARS)
                if len(dc) and (dc.ts[-1] - self.day_off) % DAY_MS != 0:
                    raise RuntimeError("velas diarias no alineadas con la frontera de día configurada "
                                       "(revisa DAY_OFFSET_MIN)")
                dser = DailySeries(dc, cfg.ps_lookback, fetched_ms=now_ms)
                self.daily[sym] = dser
            ctx = dser.ctx_for_day(need_day)
            if ctx is None:
                return 0  # sin histórico diario suficiente (moneda nueva)
            if cfg.min_daily_vol_usdt > 0 and not self.store.has_pos(sym):  # nunca se abandona una posición abierta
                if ctx.avg_vol is None or ctx.avg_vol * ctx.prior_close < cfg.min_daily_vol_usdt:
                    return 0

            st = self.store.sym(sym)
            need_bars = int((last_closed_open - need_day) // tf_ms) + 1  # velas de hoy
            if st.last_ts > 0:  # recuperación si el bot estuvo parado
                need_bars = max(need_bars, int((last_closed_open - st.last_ts) // tf_ms) + 1)
            limit = max(120, min(1440, need_bars + cfg.warmup_bars))

            candles = await self.bx.klines(sym, cfg.timeframe, limit)
            ic = candles.closed_only(now_ms, tf_ms)
            if len(ic) < MIN_INTRADAY_BARS:
                return 0

            events, info = analyze(sym, ic, dser, cfg, st, now_ms, meta, self.day_off)
            if info["ep_armed"] or info["ps_ctx"]:
                self.hot.add(sym)
            else:
                self.hot.discard(sym)
            if events:
                self._handle(events)
                self.store.save()
            return len(events)

    def _handle(self, events: List[Event]) -> None:
        for ev in events:
            if ev.kind == "entry":
                self.store.add_signal(ev.data["setup"], ev.ts)
            elif ev.kind in EXIT_KINDS:
                self.store.add_closed(ev.data["total_r"])
            log.info("EVENTO %-8s %s", ev.kind, ev.symbol)
            try:
                self.notifier.send(format_event(ev, self.cfg))
            except Exception:  # noqa: BLE001
                log.exception("No se pudo formatear el evento %s %s", ev.kind, ev.symbol)

    # ── un ciclo de escaneo ────────────────────────────────────

    async def scan_once(self) -> None:
        t0 = time.monotonic()
        now_ms = self.bx.now_ms()
        syms = sorted(self.universe, key=lambda s: 0 if (s in self.hot or self.store.has_pos(s)) else 1)
        results = await asyncio.gather(*(self._process(s, now_ms) for s in syms), return_exceptions=True)

        ok = err = nev = 0
        shown = 0
        errs: Counter = Counter()
        for s, r in zip(syms, results):
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    continue
                err += 1
                errs[f"{type(r).__name__}: {str(r)[:110]}"] += 1
                if shown < 5:
                    log.warning("Error procesando %s: %s: %s", s, type(r).__name__, r)
                    shown += 1
                else:
                    log.debug("Error procesando %s: %s", s, r, exc_info=r)
            else:
                ok += 1
                nev += int(r or 0)

        self.store.save()
        self.scan_no += 1
        secs = time.monotonic() - t0
        self.last_scan = {"secs": secs, "ok": ok, "err": err, "events": nev}
        log.info("Escaneo #%d: %d símbolos OK, %d con error, %d eventos, %.1fs, %d peticiones, %d en vigilancia",
                 self.scan_no, ok, err, nev, secs, getattr(self.bx, "requests", 0), len(self.hot))

        if syms and err > max(5, len(syms) // 2):
            top, n_top = errs.most_common(1)[0]
            self._alert("⚠️ <b>BingX responde mal</b>: "
                        f"{err} de {len(syms)} símbolos con error en el último escaneo.\n"
                        f"Error más frecuente (×{n_top}): <code>{esc(top)}</code>\n"
                        "Si persiste, revisa la región de Railway o los límites de la API.")
        if secs > self.cfg.tf_sec:
            log.warning("El escaneo (%.0fs) tarda más que el timeframe (%ds): baja el nº de símbolos "
                        "o usa un timeframe mayor", secs, self.cfg.tf_sec)

    def _alert(self, text: str) -> None:
        if time.time() - self._last_alert > 3600:
            self._last_alert = time.time()
            self.notifier.send(text)

    def _maybe_heartbeat(self) -> None:
        hrs = self.cfg.heartbeat_hours
        if hrs <= 0 or time.time() - self.last_heartbeat < hrs * 3600:
            return
        self.last_heartbeat = time.time()
        now_ms = self.bx.now_ms()
        self.notifier.send(fmt_heartbeat(
            self.cfg, now_ms, self.scan_no, self.last_scan, len(self.universe),
            self.store.signals_today(now_ms), self.store.open_positions(), self.store.stats,
        ))

    async def detect_day_offset(self) -> None:
        """Alinea el 'día' de la estrategia con las velas diarias de BingX (normalmente 00:00 UTC)."""
        if self.cfg.day_offset_min >= 0:
            log.info("Frontera diaria fijada por configuración: +%d min desde 00:00 UTC", self.cfg.day_offset_min)
            return
        probe = "BTC-USDT" if "BTC-USDT" in self.universe else next(iter(self.universe))
        try:
            dc = await self.bx.klines(probe, "1d", 5)
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo comprobar la frontera diaria con %s (%s); se asume 00:00 UTC", probe, e)
            return
        if len(dc):
            self.day_off = dc.ts[-1] % DAY_MS
            if self.day_off:
                log.warning("Las velas diarias de BingX abren a las %02d:%02d UTC (no a las 00:00): "
                            "el día de la estrategia se alinea con ellas",
                            self.day_off // 3_600_000, self.day_off % 3_600_000 // 60_000)
            else:
                log.info("Las velas diarias de BingX abren a las 00:00 UTC")

    # ── bucle principal ────────────────────────────────────────

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def _sleep_until_next_bar(self) -> None:
        tf = self.cfg.tf_sec
        now = self.bx.now_ms() / 1000.0
        target = (int(now // tf) + 1) * tf + self.cfg.scan_delay_sec
        await self._wait(target - now)

    async def _settle_first_scan(self) -> None:
        """Si el bot arranca justo al cerrar una vela, espera a que BingX la consolide."""
        since_close = (self.bx.now_ms() % self.cfg.tf_ms) / 1000.0
        if since_close < self.cfg.scan_delay_sec:
            await self._wait(self.cfg.scan_delay_sec - since_close)

    async def _housekeeping(self) -> None:
        if time.time() - self._last_sync > 3600:
            await self.bx.sync_time()
            self._last_sync = time.time()
        if time.time() - self.universe_ts > 3600:
            try:
                await self.refresh_universe()
                log.info("Universo actualizado: %d símbolos", len(self.universe))
            except Exception as e:  # noqa: BLE001
                log.warning("No se pudo actualizar la lista de contratos (%s); se mantiene la anterior", e)

    async def run(self, once: bool = False) -> None:
        await self.bx.sync_time()
        self._last_sync = time.time()
        while not self.stop.is_set():
            try:
                await self.refresh_universe()
                break
            except Exception as e:  # noqa: BLE001
                log.error("No se pudo cargar la lista de contratos de BingX (%s). Reintento en 30 s", e)
                await self._wait(30)
        if self.stop.is_set():
            return
        log.info("Universo: %d símbolos (excluidos por filtro: %d)", len(self.universe), self.excluded)
        await self.detect_day_offset()
        self.notifier.send(fmt_start(self.cfg, len(self.universe), len(self.store.open_positions()), __version__))

        first = True
        while not self.stop.is_set():
            if not first:
                await self._sleep_until_next_bar()
                if self.stop.is_set():
                    break
                await self._housekeeping()
            else:
                await self._settle_first_scan()
            first = False
            try:
                await self.scan_once()
                self._maybe_heartbeat()
            except Exception:  # noqa: BLE001
                log.exception("Error inesperado en el ciclo de escaneo; se reintenta en el siguiente")
                self._alert("⚠️ <b>Error interno</b> en un ciclo de escaneo. Revisa los logs de Railway.")
            if once:
                break


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def amain(once: bool = False) -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)
    log.info("Qullamaggie Bot v%s · TF %s · setups %s · DRY_RUN=%s",
             __version__, cfg.timeframe, cfg.active_setup, cfg.dry_run)

    store = Store(cfg.data_dir)
    notifier = Notifier(cfg)
    await notifier.start()
    async with BingXClient(cfg.rate_per_sec, base_url=cfg.bingx_base_url) as bx:
        engine = Engine(cfg, bx, store, notifier)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, engine.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await engine.run(once=once)
        except Exception:  # noqa: BLE001
            log.exception("Error fatal en el bucle principal")
            return 1
        finally:
            store.save()
            await notifier.close()
    log.info("Bot detenido")
    return 0


def main() -> None:
    once = "--once" in sys.argv
    try:
        code = asyncio.run(amain(once))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
