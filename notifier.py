"""Envío de mensajes a Telegram mediante una cola (respeta el límite de ~1 mensaje/s por chat)."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Optional

import aiohttp

from config import Config

log = logging.getLogger("telegram")

_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


class Notifier:
    api_base = "https://api.telegram.org"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=2000)
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self.sent = 0
        self.failed = 0

    async def start(self) -> None:
        if not self.cfg.dry_run:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._task = asyncio.create_task(self._worker(), name="telegram-worker")

    def send(self, text: str) -> None:
        """Encola un mensaje (no bloquea)."""
        try:
            self._q.put_nowait(text)
        except asyncio.QueueFull:
            log.error("Cola de Telegram llena: mensaje descartado")

    async def close(self, flush_timeout: float = 20.0) -> None:
        try:
            await asyncio.wait_for(self._q.join(), timeout=flush_timeout)
        except asyncio.TimeoutError:
            log.warning("Quedaron mensajes sin enviar al cerrar")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._session is not None:
            await self._session.close()

    # ── interno ────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            text = await self._q.get()
            try:
                await self._deliver(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.failed += 1
                log.error("Error inesperado enviando a Telegram: %s", type(e).__name__)
            finally:
                self._q.task_done()
            await asyncio.sleep(0.4)

    async def _deliver(self, text: str) -> None:
        if self.cfg.dry_run:
            log.info("[DRY_RUN] Mensaje de Telegram:\n%s", strip_tags(text))
            self.sent += 1
            return
        for chat in self.cfg.telegram_chat_ids:
            await self._post(chat, text)

    async def _post(self, chat_id: str, text: str) -> bool:
        assert self._session is not None
        url = f"{self.api_base}/bot{self.cfg.telegram_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, 6):
            try:
                async with self._session.post(url, json=payload) as resp:
                    status = resp.status
                    body = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Telegram: error de red (%s), intento %d/5", type(e).__name__, attempt)
                await asyncio.sleep(2 * attempt)
                continue

            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            if status == 200 and data.get("ok"):
                self.sent += 1
                return True

            desc = str(data.get("description", body[:200]))
            if status == 429:
                wait = float((data.get("parameters") or {}).get("retry_after", 5)) + 1.0
                log.warning("Telegram: límite de envío, espero %.0fs", wait)
                await asyncio.sleep(min(wait, 60.0))
                continue
            if status == 400 and "parse entities" in desc.lower() and "parse_mode" in payload:
                # HTML mal formado: se reenvía como texto plano para no perder la señal
                payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                payload["text"] = strip_tags(text)[:4096]
                continue
            if status >= 500:
                await asyncio.sleep(2 * attempt)
                continue
            log.error("Telegram rechazó el mensaje (HTTP %s): %s", status, desc)
            self.failed += 1
            return False

        self.failed += 1
        log.error("Telegram: no se pudo enviar tras 5 intentos")
        return False
