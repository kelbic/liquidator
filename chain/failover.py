"""Failover боевого READ-пути (подпись 02.08, четыре условия ревью).

Лесенка №2 (RPC_URL) -> COMBAT_FAILOVER_URLS (05.08: Alchemy №1, затем keyed dRPC №1;
состав — факт .env, не кода). Отправки сюда НЕ входят:
SEND-3 уже шлёт raw-tx в секвенсер параллельно с RPC-путём (execute.py), у холодного
nonce свой фолбэк (_NONCE_FALLBACK_RPC). Мёртвый №2 делает бота не немым, а СЛЕПЫМ —
пакет страхует чтения: сборку проб, цены, реситы, nonce-пере-чтение.

Условие 1 (латентность): health-check живёт ТОЛЬКО в фоновом треде; горячий путь читает
атомарный указатель (_idx, int-присвоение под GIL) — ноль лишних RPC перед вызовом.
Условие 2 (пин): вперёд — по серии жёстких отказов (conn/timeout/HTTP, >= COMBAT_HC_FAILS
подряд; дрожание латентности исключение не бросает и потому не считается); назад — только
при 0 активных spec-fire окон И здоровье primary >= COMBAT_BACK_HEALTHY_SEC.
Условие 3 (квоты): если боевой указатель встал на URL bg-пути — bg_shed() = True,
фон уступает ключ (кэш-рефреш скипается, predict-полл прореживается х4).
Условие 4 (наблюдаемость): каждый свитч — TG + журнал; откат одной строкой COMBAT_FAILOVER=0
(тред не стартует, указатель прибит к primary = прежнее поведение).

Head-guard (read-after-write, «block not found» 07-23): кандидат принимается только если
его head >= последнего виденного нами блока — голова обязана покрыть уже прочитанное.
Receipt-полл и nonce ездят на том же указателе автоматически (все через self.rpc).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from chain.rpc import BaseRpc, redact_clip

log = logging.getLogger("liquidator")

COMBAT_FAILOVER = os.environ.get("COMBAT_FAILOVER", "1") == "1"
COMBAT_FAILOVER_URLS = [u.strip() for u in os.environ.get("COMBAT_FAILOVER_URLS", "").split(",")
                        if u.strip()]
COMBAT_HC_SEC = float(os.environ.get("COMBAT_HC_SEC", "5"))
COMBAT_HC_FAILS = int(os.environ.get("COMBAT_HC_FAILS", "3"))
COMBAT_BACK_HEALTHY_SEC = float(os.environ.get("COMBAT_BACK_HEALTHY_SEC", "60"))

_ACTIVE = None                    # синглтон для bg_shed()/set_windows_probe() из hotpath/predict


def bg_shed() -> bool:
    """Бой уехал на ключ bg-пути -> фон уступает. Дёшево: два атрибута + сравнение строк."""
    a = _ACTIVE
    return a is not None and a._bg_collision()


def set_windows_probe(fn) -> None:
    a = _ACTIVE
    if a is not None:
        a._windows_probe = fn


class FailoverRpc(BaseRpc):
    """Дроп-ин на место боевого BaseRpc: contract()/to_bytes32() наследуются и ходят через
    _web3(), который делегирует текущему живому провайдеру."""

    def __init__(self, primary: BaseRpc, alternates, *, bg_url: str = "", alerter=None,
                 cfg=None, now_fn=time.monotonic):
        self.providers = [primary] + list(alternates)
        self.chain_id = primary.chain_id
        self.request_timeout = getattr(primary, "request_timeout", None)
        self._idx = 0
        self._bg_url = (bg_url or "").strip().rstrip("/")
        self._alerter = alerter
        self._cfg = cfg
        self._now = now_fn
        self._fail_streak = 0
        self._last_head = 0
        self._primary_ok_since = None
        self._windows_probe = None
        self._switch_lock = threading.Lock()      # только свитч; горячий путь лочек не берёт
        self._stop = threading.Event()
        self.stats = {"switches": 0, "checks": 0, "check_fails": 0}

    # -- горячий путь: атомарный указатель, ноль RPC --------------------------------------
    @property
    def rpc_url(self):
        return self.providers[self._idx].rpc_url

    @rpc_url.setter
    def rpc_url(self, _v):                        # BaseRpc.__init__ не зовём; сеттер — заглушка
        pass

    def _web3(self):
        return self.providers[self._idx]._web3()

    def _bg_collision(self) -> bool:
        return (self._idx != 0 and bool(self._bg_url)
                and self.providers[self._idx].rpc_url.rstrip("/") == self._bg_url)

    # -- фоновый health-цикл ---------------------------------------------------------------
    def start(self):
        global _ACTIVE
        _ACTIVE = self
        if not COMBAT_FAILOVER or len(self.providers) < 2:
            log.info("COMBAT-FAILOVER выключен/без кандидатов — указатель прибит к primary")
            return self
        threading.Thread(target=self._loop, daemon=True, name="combat-failover").start()
        log.info("COMBAT-FAILOVER: primary + %d кандидатов, hc=%.0fс, гистерезис=%d, назад>=%.0fс",
                 len(self.providers) - 1, COMBAT_HC_SEC, COMBAT_HC_FAILS, COMBAT_BACK_HEALTHY_SEC)
        return self

    def _loop(self):
        while not self._stop.wait(COMBAT_HC_SEC):
            try:
                self._check_once()
            except Exception:                     # health-цикл не умирает никогда
                pass

    def _head(self, p) -> int:
        return int(p._web3().eth.block_number)

    def _check_once(self):
        """Один тик: health текущего + (вне штата) проба primary на возврат."""
        self.stats["checks"] += 1
        cur = self.providers[self._idx]
        try:
            bn = self._head(cur)
            self._last_head = max(self._last_head, bn)
            self._fail_streak = 0
        except Exception as e:                    # transport/HTTP = жёсткий отказ; латентность
            self._fail_streak += 1                # исключений не бросает и сюда не попадает
            self.stats["check_fails"] += 1
            log.warning("COMBAT-FAILOVER: health-fail #%d на %s: %s",
                        self._fail_streak, _short(cur.rpc_url), redact_clip(e, 100))
            if COMBAT_FAILOVER and self._fail_streak >= COMBAT_HC_FAILS:
                self._failover("серия %d жёстких отказов" % self._fail_streak)
                return
        if self._idx != 0 and COMBAT_FAILOVER:
            self._try_return()

    def _failover(self, reason: str) -> bool:
        for j in [k for k in range(len(self.providers)) if k != self._idx]:
            try:
                bn = self._head(self.providers[j])
            except Exception:
                continue
            if bn >= self._last_head:             # head-guard: голова покрывает виденное
                self._switch(j, reason)
                return True
            log.warning("COMBAT-FAILOVER: кандидат %s отстаёт (head=%d < %d) — пропуск",
                        _short(self.providers[j].rpc_url), bn, self._last_head)
        # #105: ВОТ ЗДЕСЬ человек нужен — переключаться некуда, и следующий отказ боевого
        # ключа оставит нас слепыми. Раньше этот случай молчал в журнале, а в телеграм уходило
        # рутинное переключение, которое лечится само. Тревоги были ровно наоборот.
        log.warning("COMBAT-FAILOVER: все кандидаты мертвы/отстают — остаёмся на %s",
                    _short(self.providers[self._idx].rpc_url))
        self._alert_all_dead()
        return False

    def _alert_all_dead(self):
        """Тревога «резерва не осталось» — с защёлкой, чтобы не звонить на каждом опросе.

        Защёлка снимается, когда снова нашёлся живой кандидат (см. _switch): состояние
        изменилось — значит и право позвонить вернулось. Без защёлки один затяжной отказ
        провайдера дал бы сотни сообщений, и следующая настоящая беда утонула бы в них."""
        if self._alerter is None or getattr(self, "_all_dead_latched", False):
            return
        self._all_dead_latched = True
        try:
            self._alerter.send_async(
                "⛑ RPC: РЕЗЕРВА НЕ ОСТАЛОСЬ — все кандидаты мертвы/отстают, сидим на %s. "
                "Следующий отказ = слепота." % _short(self.providers[self._idx].rpc_url))
        except Exception:
            pass

    def _try_return(self):
        """Назад на primary: только здоровый >= BACK_HEALTHY_SEC подряд И 0 активных окон."""
        try:
            bn0 = self._head(self.providers[0])
            if bn0 < self._last_head:
                self._primary_ok_since = None
                return
            self._last_head = max(self._last_head, bn0)
            if self._primary_ok_since is None:
                self._primary_ok_since = self._now()
        except Exception:
            self._primary_ok_since = None
            return
        if self._now() - self._primary_ok_since < COMBAT_BACK_HEALTHY_SEC:
            return
        try:
            wa = int(self._windows_probe()) if self._windows_probe else 0
        except Exception:
            wa = 0
        if wa > 0:                                # посреди окна назад не ходим вообще
            return
        self._switch(0, "primary здоров %.0fс, окон 0" % COMBAT_BACK_HEALTHY_SEC)

    def _switch(self, j: int, reason: str):
        with self._switch_lock:
            old = self.providers[self._idx].rpc_url
            self._idx = j                          # атомарное присвоение — горячий путь видит сразу
            self._fail_streak = 0
            self._primary_ok_since = None
            self._all_dead_latched = False      # #105: нашёлся живой — право звонить вернулось
            self.stats["switches"] += 1
        new = self.providers[j].rpc_url
        try:                                       # nonce мог быть прочитан со старой ноды
            from chain.execute import canary_invalidate_nonce
            if self._cfg is not None:
                canary_invalidate_nonce(self, self._cfg)
        except Exception:
            pass
        msg = "COMBAT-FAILOVER: %s -> %s (%s)%s" % (
            _short(old), _short(new), reason,
            " | bg уступает ключ" if self._bg_collision() else "")
        log.warning(msg)
        # #105: рутинное переключение в ТЕЛЕГРАМ БОЛЬШЕ НЕ ИДЁТ.
        #
        # Канон проекта: тревога только там, где нужен ЧЕЛОВЕК. Здесь человек не нужен ни в
        # одном сценарии — резерв подхватывает сам, деньги не теряются. Замер 11.08: 77
        # переключений за сутки, из них 26 за один час; каждое слало сообщение владельцу.
        # Плюс это осциллирующая величина, а по нашему же канону повтор тревоги допустим лишь
        # на ЭСКАЛАЦИИ, а не на каждом зубе пилы.
        #
        # Диагностическая ценность при этом НЕ теряется: строка остаётся в журнале, а частота
        # переключений — термометр нагрузки на боевой ключ (наш собственный огонь его и топит,
        # см. #100). Мерить её надо после каждой правки темпа — но глазами, а не будильником.


def _short(url: str) -> str:
    try:
        host = url.split("://", 1)[1].split("/", 1)[0]
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return "%s/…%s" % (host, tail[-4:]) if tail and tail != host else host
    except Exception:
        return url[:40]


def build_failover(primary: BaseRpc, cfg, alerter=None):
    """Обёртка боевого rpc. Пустой COMBAT_FAILOVER_URLS или откат-флаг = primary как был."""
    if primary is None or not COMBAT_FAILOVER or not COMBAT_FAILOVER_URLS:
        return primary
    alts = [BaseRpc(u, primary.chain_id, request_timeout=5) for u in COMBAT_FAILOVER_URLS]
    return FailoverRpc(primary, alts, bg_url=str(getattr(cfg, "bg_rpc_url", "") or ""),
                       alerter=alerter, cfg=cfg).start()
