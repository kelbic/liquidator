"""Telegram admin alerts with antiflood (mirrors essayist's infra_error alerting)."""
from __future__ import annotations
import time
import urllib.parse
import urllib.request


class Alerter:
    def __init__(self, bot_token: str, admin_id: int, antiflood_sec: int = 600):
        self.token = bot_token
        self.admin_id = admin_id
        self.antiflood_sec = antiflood_sec
        self._last: dict[str, float] = {}

    def send(self, text: str, key: str = "default") -> bool:
        """Send to admin. Antiflood: at most one message per `key` per window."""
        now = time.time()
        if now - self._last.get(key, 0) < self.antiflood_sec:
            return False
        self._last[key] = now
        if not self.token or not self.admin_id:
            return False
        try:
            data = urllib.parse.urlencode({"chat_id": self.admin_id, "text": text}).encode()
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
                return r.status == 200
        except Exception:
            return False
