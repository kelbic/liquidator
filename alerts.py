"""Telegram admin alerts with antiflood (mirrors essayist's infra_error alerting)."""
from __future__ import annotations
import threading
import time
import urllib.parse
import urllib.request


class Alerter:
    def __init__(self, bot_token: str, admin_id: int, antiflood_sec: int = 600, tag: str = ""):
        self.token = bot_token
        self.admin_id = admin_id
        self.antiflood_sec = antiflood_sec
        self.tag = tag              # префикс отправителя в ОБЩЕМ чате флота (напр. "base") -> "[base] ..."
        self._last: dict[str, float] = {}

    def send_async(self, text: str, key: str = "default") -> None:
        """Fire-and-forget в daemon-треде — НИКОГДА не блокирует вызывающего (горячий путь / луп).
        Синхронный send делает urlopen(timeout=10): в блок-лупе / hot-воркере это до 10с блокировки
        на выстреле/каскаде -> пропуск блоков. Здесь доставка+antiflood уходят с потока, возврат
        отбрасывается. Гонка на self._last безобидна (изредка дубль-алерт). Скрипты (daily_summary)
        зовут sync send ради возврата — их не трогаем."""
        threading.Thread(target=self.send, args=(text, key), daemon=True).start()

    def send(self, text: str, key: str = "default") -> bool:
        """Send to admin. Antiflood: at most one message per `key` per window."""
        now = time.time()
        if now - self._last.get(key, 0) < self.antiflood_sec:
            return False
        self._last[key] = now
        if not self.token or not self.admin_id:
            return False
        if self.tag:
            text = f"[{self.tag}] {text}"
        return self._post(self.admin_id, text)

    def _post(self, chat_id, text: str) -> bool:
        if not self.token or not chat_id:
            return False
        try:
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
                return r.status == 200
        except Exception:
            return False
