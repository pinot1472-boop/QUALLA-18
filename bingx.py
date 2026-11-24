"""Cliente de la API PÚBLICA de BingX (futuros perpetuos USDT-M). No necesita API keys.

Endpoints usados:
  GET /openApi/swap/v2/quote/contracts   lista de contratos
  GET /openApi/swap/v3/quote/klines      velas (con fallback a v2 si v3 no responde)
  GET /openApi/swap/v2/server/time       hora del servidor (para detectar velas sin cerrar)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from models import Candles

log = logging.getLogger("bingx")

BASE_URL = "https://open-api.bingx.com"
KLINE_PATHS = ("/openApi/swap/v3/quote/klines", "/openApi/swap/v2/quote/klines")
RETRY_CODES = {100410, 100500, 100503}  # límite de frecuencia / servidor ocupado


class BingXError(Exception):
    def __init__(self, msg: str, status: Optional[int] = None, code: Optional[int] = None):
        super().__init__(msg)
        self.status = status
        self.code = code


class RateLimiter:
    """Espacia las peticiones de forma uniforme (máx. rate_per_sec por segundo)."""

    def __init__(self, rate_per_sec: float):
        self._interval = 1.0 / max(rate_per_sec, 0.1)
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = max(now, self._next) + self._interval


class BingXClient:
    def __init__(self, rate_per_sec: float = 8.0, timeout: float = 20.0, base_url: str = BASE_URL):
        self._base = base_url.rstrip("/")
        self._limiter = RateLimiter(rate_per_sec)
        self._timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._cooldown_until = 0.0
        self._kline_idx = 0
        self._kline_ok = False  # True cuando ya sabemos qué versión de endpoint funciona
        self.time_offset_ms = 0
        self.requests = 0

    async def __aenter__(self) -> "BingXClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            headers={"User-Agent": "qullamaggie-bot/1.0", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session is not None:
            await self._session.close()

    # ── utilidades ─────────────────────────────────────────────

    def now_ms(self) -> int:
        """Hora actual en ms, corregida con el desfase respecto al servidor de BingX."""
        return int(time.time() * 1000) + self.time_offset_ms

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None, retries: int = 4) -> Any:
        assert self._session is not None
        last: Optional[Exception] = None
        for attempt in range(retries):
            wait = self._cooldown_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self._limiter.acquire()
            self.requests += 1
            try:
                async with self._session.get(self._base + path, params=params) as resp:
                    status = resp.status
                    text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = BingXError(f"red: {type(e).__name__}")
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            if status == 429 or status >= 500:
                self._cooldown_until = time.monotonic() + 2.0 * (attempt + 1)
                last = BingXError(f"HTTP {status}", status=status)
                continue
            try:
                data = json.loads(text)
            except ValueError:
                raise BingXError(f"respuesta no JSON (HTTP {status}): {text[:120]!r}", status=status)

            code: Optional[int] = None
            if isinstance(data, dict):
                try:
                    code = int(data.get("code", 0))
                except (TypeError, ValueError):
                    code = 0
            if code in RETRY_CODES:
                self._cooldown_until = time.monotonic() + 2.0 * (attempt + 1)
                last = BingXError(f"código {code}: {data.get('msg')}", status=status, code=code)
                continue
            if status >= 400:
                hint = " (¿bloqueo geográfico? prueba otra región en Railway)" if status in (403, 451) else ""
                raise BingXError(f"HTTP {status}{hint}", status=status, code=code)
            if code != 0:
                raise BingXError(f"código {code}: {data.get('msg')}", status=status, code=code)
            return data

        raise last or BingXError("sin respuesta")

    # ── API pública ────────────────────────────────────────────

    async def sync_time(self) -> None:
        try:
            data = await self._get("/openApi/swap/v2/server/time", retries=2)
            server = int(data["data"]["serverTime"])
            self.time_offset_ms = server - int(time.time() * 1000)
            log.info("Desfase de reloj respecto a BingX: %+d ms", self.time_offset_ms)
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo sincronizar la hora con BingX (%s); se usa el reloj local", e)

    async def contracts(self) -> List[Dict[str, Any]]:
        data = await self._get("/openApi/swap/v2/quote/contracts")
        rows = data.get("data")
        if not isinstance(rows, list) or not rows:
            raise BingXError("lista de contratos vacía")
        return rows

    async def klines(self, symbol: str, interval: str, limit: int) -> Candles:
        """Velas ordenadas de antigua a reciente (incluye la vela en formación, si la hay)."""
        params = {"symbol": symbol, "interval": interval, "limit": max(2, min(int(limit), 1440))}
        last: Optional[Exception] = None
        for k in range(len(KLINE_PATHS)):
            idx = (self._kline_idx + k) % len(KLINE_PATHS)
            try:
                data = await self._get(KLINE_PATHS[idx], params)
            except BingXError as e:
                last = e
                if self._kline_ok:
                    raise  # el endpoint ya funcionó antes: el fallo es del símbolo o transitorio
                continue
            self._kline_idx = idx
            self._kline_ok = True
            return parse_klines(data.get("data"))
        raise last or BingXError("klines: sin respuesta")


def parse_klines(rows: Any) -> Candles:
    """Acepta velas como dict (swap v2/v3) o como lista [t,o,h,l,c,v]. Ordena y elimina duplicados."""
    parsed: Dict[int, tuple] = {}
    if not isinstance(rows, list):
        return Candles()
    for r in rows:
        try:
            if isinstance(r, dict):
                t = int(r.get("time", r.get("openTime", r.get("t"))))
                o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
                v = float(r.get("volume", 0) or 0)
            else:
                t = int(r[0])
                o, h, l, c, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if h < l or c <= 0:
            continue
        parsed[t] = (o, h, l, c, v)
    out = Candles()
    for t in sorted(parsed):
        o, h, l, c, v = parsed[t]
        out.ts.append(t)
        out.o.append(o)
        out.h.append(h)
        out.l.append(l)
        out.c.append(c)
        out.v.append(v)
    return out
