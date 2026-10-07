"""Alerts. Telegram if configured, otherwise stdout. Also reads Telegram commands."""

from __future__ import annotations

import logging
from typing import Callable

from . import http

log = logging.getLogger("goldbot")

COMMANDS = {"/stop", "/resume", "/status", "/help"}


class Notifier:
    def send(self, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def commands(self, offset: int) -> tuple[list[str], int]:
        """New commands from the authorised chat, and the next update offset."""
        return [], offset


class ConsoleNotifier(Notifier):
    def __init__(self, quiet: bool = False):
        self.sent: list[str] = []
        self.quiet = quiet

    def send(self, text: str) -> None:
        self.sent.append(text)
        if not self.quiet:
            print(f"[alert]\n{text}\n", flush=True)


class TelegramNotifier(Notifier):
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str, req: Callable = http.request):
        self.token, self.chat_id, self._req = token, str(chat_id), req

    def _url(self, method: str) -> str:
        return self.API.format(token=self.token, method=method)

    def send(self, text: str) -> None:
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
            try:
                self._req("POST", self._url("sendMessage"), json_body={
                    "chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True})
            except Exception as e:  # noqa: BLE001  never let alerting crash trading logic
                log.error("telegram send failed: %s", str(e).replace(self.token, "***"))

    def commands(self, offset: int) -> tuple[list[str], int]:
        try:
            data = self._req("GET", self._url("getUpdates"),
                             params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'}).json()
        except Exception as e:  # noqa: BLE001
            log.error("telegram getUpdates failed: %s", str(e).replace(self.token, "***"))
            return [], offset
        cmds = []
        for upd in data.get("result", []):
            offset = max(offset, upd["update_id"] + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                continue  # ignore everyone except the configured chat
            text = (msg.get("text") or "").strip().split()
            if text:
                cmd = text[0].split("@")[0].lower()
                if cmd in COMMANDS:
                    cmds.append(cmd)
        return cmds, offset


def make_notifier(cfg) -> Notifier:
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        return TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: alerts go to stdout only")
    return ConsoleNotifier()
