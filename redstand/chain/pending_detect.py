"""PENDING-DETECT (пакет A, план ревьювера 04.08): детект транзитов по ФОРМИРУЕМОМУ блоку.

Гонка №2 (блок 49410446) проиграна не скоростью и не tip: победители (0.011 gwei) сели ТЕМ ЖЕ
блоком, что transmit Chainlink (idx 3985/3995/3998, FIFO) — они видели транзит в блоке under
construction (Flashblocks ~200мс гранулярность). Наши on-chain пробы тестируют ЗАПЕЧАТАННОЕ
состояние = минимум блок опоздания структурно. Этот слой опрашивает тег "pending" на ОТДЕЛЬНОМ
провайдере (A0-замер 04.08 ИЗ ШАРЛОТТЫ: mainnet.base.org p50/p95 = 53/62мс + рост pending-
состояния виден, бесключевой => CU=0; nodies 230/360мс — у ревьювера он был быстр из ЕГО точки,
гео-урок; Alchemy запрещён КОДОМ — боевые ключи, тонкий CU-запас):

  ярус 1 (дешёвый): latestRoundData прокси-фида каждого АРМЛЕННОГО окна по "pending",
    TIER1_HZ; roundId/updatedAt изменился => транзит уже в формируемом блоке;
  ярус 2 (точный): eth_call собранной пробы окна (sfl.probe_board) по "pending" — реверт
    ('healthy') = этот транзит жертву не флипнул; успех = жертва ликвидируема В ЭТОМ блоке;
  подавление (A2): Liquidate-event жертвы в запечатанном чейне (getLogs, этот же провайдер)
    => конкурент уже забрал — выстрел глушится (закрыт бэклог suppression post-Liquidate);
  handoff (A2): атомарный флаг sfl.pending_fire[feed] -> СУЩЕСТВУЮЩИЙ fire-путь окна стреляет
    немедленно (send_spec_fire_probe / SEND-3, единый nonce — без изменений).

ГАРАНТИИ (A4): свой демон-тред + свой raw-HTTP клиент (urllib; НОЛЬ общих локов с fire-путём —
sfl._windows/probe_board/pending_fire читаются/пишутся GIL-атомарными операциями без локов);
цикл READ-ONLY по конструкции — не шлёт tx, не трогает nonce, не подписывает; вне армленных
окон молчит (CU=0); health-gate: HC_FAILS циклов подряд без единого успеха -> ротация
провайдера, все мертвы -> healthy()=False -> лестница on-chain проб В ОКНАХ сама возвращается
на полную каденсию (A3-фолбэк; хаос-тест A6 в test_pending_detect.py).
Метрики: строка "PENDING stats" раз в STATS_SEC при активности — t1/t2/handoffs/suppressed/
errors + запросы по провайдерам (A5: pending-трафик отдельной строкой, ноль на боевых ключах).
Откат: PENDING_DETECT=0 (см. docs/ROLLBACKS.md).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from chain.rpc import redact_clip

log = logging.getLogger("liq.pending")

PENDING_DETECT = os.environ.get("PENDING_DETECT", "1").lower() not in ("0", "false", "no")
# primary/backup по p95 A0-замера, НЕ по названию (правило плана): base.org 62мс -> primary;
# nodies жив, но 360мс и без наблюдаемого роста pending -> хвостовой backup (детект деградирует
# к реактивной семантике, но не умирает). dRPC keyed добавляется ТОЛЬКО через .env (CU-бюджет).
PENDING_RPC_URLS = os.environ.get("PENDING_RPC_URLS",
                                  "https://mainnet.base.org,https://base-pokt.nodies.app")
PENDING_TIER1_HZ = float(os.environ.get("PENDING_TIER1_HZ", "4.0"))
# СТРАЙП по провайдерам (боевой урок 04.08 07:20, первое же окно): base.org режет выдержанные
# 4Гц (19/24 запросов = 429; A0-залп это НЕ ловит — лимит по ОКНУ времени, не по RTT). Каждый
# тик уходит СЛЕДУЮЩЕМУ живому провайдеру (round-robin), 429 = кулдаун RL_COOLDOWN этому
# провайдеру (не «смерть»), транспорт-фейлы DEAD_FAILS подряд = кулдаун DEAD_COOLDOWN. При
# 3 провайдерах каждый видит ~1.3Гц — под публичные лимиты. Все остыли -> тик пропущен ->
# healthy() гаснет -> лестница on-chain проб сама на полной каденсии (A3/A6 без изменений).
PENDING_RL_COOLDOWN_SEC = float(os.environ.get("PENDING_RL_COOLDOWN_SEC", "5.0"))
PENDING_DEAD_FAILS = int(os.environ.get("PENDING_DEAD_FAILS", "3"))
PENDING_DEAD_COOLDOWN_SEC = float(os.environ.get("PENDING_DEAD_COOLDOWN_SEC", "30.0"))
PENDING_HC_FAILS = int(os.environ.get("PENDING_HC_FAILS", "3"))   # легаси-алиас (тесты/откат)
PENDING_STALE_SEC = float(os.environ.get("PENDING_STALE_SEC", "3.0"))
PENDING_BOARD_MAX_AGE_SEC = float(os.environ.get("PENDING_BOARD_MAX_AGE_SEC", "10.0"))
PENDING_HANDOFF_CD_SEC = float(os.environ.get("PENDING_HANDOFF_CD_SEC", "5.0"))
PENDING_SUPPRESS_SEC = float(os.environ.get("PENDING_SUPPRESS_SEC", "30.0"))
PENDING_STATS_SEC = float(os.environ.get("PENDING_STATS_SEC", "600.0"))
PENDING_HTTP_TIMEOUT = float(os.environ.get("PENDING_HTTP_TIMEOUT", "2.5"))

_LATEST_ROUND_DATA = "0xfeaf968c"          # latestRoundData()
_HDRS = {"Content-Type": "application/json",
         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}   # урок SEND-3: urllib-UA ловит 403


class _Revert(Exception):
    """eth_call вернул execution-ошибку (реверт) — семантика 'мимо', НЕ сбой транспорта."""


class _RateLimited(Exception):
    """429/rate-limit: провайдер жив, но остывает — тик уходит следующему в страйпе."""


class PendingDetect:
    """Демон pending-детекта. Все внешние вызовы — через швы (http_fn/now/sleep) для юнитов."""

    def __init__(self, cfg, sfl, proxies_by_label: dict, urls=None,
                 http_fn=None, now=time.monotonic, log_fn=None):
        self.cfg, self.sfl = cfg, sfl
        self.proxies = {str(k).upper(): v for k, v in (proxies_by_label or {}).items()}
        raw = urls if urls is not None else [u.strip() for u in PENDING_RPC_URLS.split(",")]
        self.urls = []
        for u in raw:
            if not u:
                continue
            if "alchemy" in u.lower():
                # ЗАПРЕТ плана (A0): pending-цикл НИКОГДА не ходит на боевые Alchemy-ключи.
                log.error("PENDING: url отклонён КОДОМ — Alchemy в pending-цикле запрещён (%s…)", u[:34])
                continue
            self.urls.append(u)
        self._rr = 0                       # round-robin страйпа запросов по провайдерам
        self._cool: dict = {}              # idx -> ts, до которого провайдер остывает (429/смерть)
        self._pfails: dict = {}            # idx -> транспорт-фейлы подряд (сброс успехом)
        self._errlog_ts = 0.0              # троттлинг причин ошибок в лог (урок дебага 07:20)
        self._healthy_ts = -1e9
        self._base: dict = {}              # label -> (roundId, updatedAt) базлайн pending
        self._handoff_cd: dict = {}        # label -> ts, до которого handoff не повторяем
        self._sup_until: dict = {}         # (mid, b) -> ts подавления (конкурент забрал)
        self._liq_sig = None               # keccak Liquidate(...) — лениво
        self._http = http_fn or self._http_post
        self._now = now
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._stop = threading.Event()
        self._last_stats = (0.0, 0)
        self.stats = {"t1": 0, "t2": 0, "handoffs": 0, "suppressed": 0, "errors": 0,
                      "failovers": 0, "rl429": 0, "reqs": {}}

    # ---- транспорт ----
    def _http_post(self, url: str, body: bytes) -> dict:
        req = urllib.request.Request(url, data=body, headers=_HDRS)
        with urllib.request.urlopen(req, timeout=PENDING_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())

    def _pick(self):
        """Следующий НЕостывающий провайдер (страйп round-robin). None = все на кулдауне."""
        n = self._now()
        for k in range(len(self.urls)):
            i = (self._rr + k) % len(self.urls)
            if self._cool.get(i, 0.0) <= n:
                self._rr = i + 1
                return i
        return None

    def _host(self, i):
        return self.urls[i].split("//", 1)[-1].split("/", 1)[0]

    def _rpc(self, method: str, params: list, idx: int):
        """Один запрос через провайдера idx. 429 -> RL-кулдаун провайдеру + _RateLimited;
        прочий транспорт -> счётчик фейлов, DEAD_FAILS подряд -> длинный кулдаун. Причина
        уходит в лог троттлированно (урок 07:20: «мёртв» без причины не дебажится)."""
        url = self.urls[idx]
        host = self._host(idx)
        self.stats["reqs"][host] = self.stats["reqs"].get(host, 0) + 1
        try:
            out = self._http(url, json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode())
        except Exception as e:
            n = self._now()
            if getattr(e, "code", None) == 429:
                self._cool[idx] = n + PENDING_RL_COOLDOWN_SEC
                self.stats["rl429"] = self.stats.get("rl429", 0) + 1
                raise _RateLimited(host)
            self._pfails[idx] = self._pfails.get(idx, 0) + 1
            if self._pfails[idx] >= PENDING_DEAD_FAILS:
                self._cool[idx] = n + PENDING_DEAD_COOLDOWN_SEC
                self._pfails[idx] = 0
                self.stats["failovers"] += 1
                if n - self._errlog_ts >= 30.0:
                    self._errlog_ts = n
                    self._log("PENDING провайдер %s остыл на %.0fс: %s: %s" % (
                        host, PENDING_DEAD_COOLDOWN_SEC, type(e).__name__, redact_clip(e, 60)))
            raise
        self._pfails[idx] = 0
        if "error" in out:
            err = str(out["error"])[:120]
            if "429" in err or "rate" in err.lower():
                self._cool[idx] = self._now() + PENDING_RL_COOLDOWN_SEC
                self.stats["rl429"] = self.stats.get("rl429", 0) + 1
                raise _RateLimited(host)
            raise _Revert(err)                       # execution-ошибка (реверт) — данные, не транспорт
        return out.get("result")

    # ---- публичное здоровье (A3: его читает лестница on-chain проб) ----
    def healthy(self) -> bool:
        return self._now() - self._healthy_ts <= PENDING_STALE_SEC

    # ---- ярусы ----
    def _read_round(self, proxy: str, tag: str, idx: int):
        res = self._rpc("eth_call", [{"to": proxy, "data": _LATEST_ROUND_DATA}, tag], idx)
        raw = bytes.fromhex((res or "0x")[2:])
        if len(raw) < 160:
            raise _Revert("short latestRoundData")
        return (int.from_bytes(raw[0:32], "big"), int.from_bytes(raw[96:128], "big"))

    def _liquidated_sealed(self, borrower: str, idx: int) -> bool:
        """A2-подавление: Liquidate-event жертвы уже в ЗАПЕЧАТАННОМ чейне = конкурент успел.
        Fail-open (не смогли проверить -> стреляем; цена ошибки = один реверт ~$0.01)."""
        try:
            if self._liq_sig is None:
                from eth_utils import keccak
                self._liq_sig = "0x" + keccak(
                    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)").hex()
            from chain.execute import _MORPHO_BLUE
            head = int(self._rpc("eth_blockNumber", [], idx), 16)
            logs = self._rpc("eth_getLogs", [{
                "fromBlock": hex(max(0, head - 3)), "toBlock": "latest", "address": _MORPHO_BLUE,
                "topics": [self._liq_sig, None, None,
                           "0x" + borrower[2:].lower().rjust(64, "0")]}], idx)
            return bool(logs)
        except Exception:
            return False

    def _tier2(self, label: str):
        board = getattr(self.sfl, "probe_board", {}).get(label)
        if not board:
            return                          # окно ещё не собрало пробу — накроет on-chain лестница
        mid, b, debt, calldata, gas, ts = board
        n = self._now()
        if (n - ts > PENDING_BOARD_MAX_AGE_SEC
                or self._handoff_cd.get(label, 0.0) > n
                or self._sup_until.get((mid, b), 0.0) > n):
            return
        idx = self._pick()
        if idx is None:
            return                          # все провайдеры остывают — накроет on-chain лестница
        self.stats["t2"] += 1
        try:
            self._rpc("eth_call", [{"from": self.cfg.wallet_address,
                                    "to": self.cfg.liquidator_address,
                                    "data": calldata}, "pending"], idx)
        except _Revert:
            return                          # 'healthy' — ЭТОТ транзит жертву не флипнул
        except _RateLimited:
            return                          # тик потерян, страйп уведёт следующий к другому провайдеру
        # успех: жертва ликвидируема в формируемом блоке
        _si = self._pick()
        if self._liquidated_sealed(b, _si if _si is not None else idx):
            self.stats["suppressed"] += 1
            self._sup_until[(mid, b)] = n + PENDING_SUPPRESS_SEC
            self._log("PENDING suppress feed=%s %s/%s: Liquidate-event уже в чейне — конкурент "
                      "успел, выстрел заглушен" % (label, mid[:10], b[:10]))
            return
        self.sfl.pending_fire[label] = (mid, b, debt, n)      # атомарный handoff (A2)
        self._handoff_cd[label] = n + PENDING_HANDOFF_CD_SEC
        self.stats["handoffs"] += 1
        log.warning("PENDING HANDOFF feed=%s %s/%s debt=$%.0fk — жертва флипнута в ФОРМИРУЕМОМ "
                    "блоке, огонь передан окну", label, mid[:10], b[:10], debt / 1e3)

    def _cycle(self, feeds: list) -> bool:
        """Один тик яруса-1 по всем армленным окнам. Каждый фид — через СЛЕДУЮЩЕГО живого
        провайдера страйпа (429-кулдауны разводят каденс под публичные лимиты).
        True = был хотя бы один успешный запрос."""
        ok = False
        for lbl in feeds:
            proxy = self.proxies.get(lbl)
            if not proxy:
                continue
            idx = self._pick()
            if idx is None:
                break                       # все провайдеры остывают — тик пропущен, healthy угаснет
            try:
                cur = self._read_round(proxy, "pending", idx)
                self.stats["t1"] += 1
                ok = True
                base = self._base.get(lbl)
                if base is None:
                    # первый тик окна: транзит мог УЖЕ сидеть в формируемом блоке — сверяем
                    # pending против запечатанного latest, расхождение = сразу ярус-2
                    sealed = self._read_round(proxy, "latest", idx)
                    self._base[lbl] = cur
                    if cur != sealed:
                        self._tier2(lbl)
                elif cur != base:
                    self._base[lbl] = cur
                    self._tier2(lbl)
            except (_Revert, _RateLimited):
                self.stats["errors"] += 1   # данные/лимит — тик фида потерян, цикл живёт
            except Exception:
                self.stats["errors"] += 1   # транспорт: кулдаун навесил _rpc, идём к следующему фиду
        return ok

    def _maybe_stats(self):
        n = self._now()
        ts, seen = self._last_stats
        total = self.stats["t1"] + self.stats["t2"]
        if n - ts >= PENDING_STATS_SEC and total > seen:
            self._last_stats = (n, total)
            self._log("PENDING stats: t1=%d t2=%d handoffs=%d suppressed=%d errors=%d "
                      "rl429=%d failovers=%d reqs=%s healthy=%s" % (
                          self.stats["t1"], self.stats["t2"], self.stats["handoffs"],
                          self.stats["suppressed"], self.stats["errors"], self.stats["rl429"],
                          self.stats["failovers"], self.stats["reqs"], self.healthy()))

    def _loop(self):
        period = 1.0 / max(0.5, PENDING_TIER1_HZ)
        while not self._stop.is_set():
            try:
                feeds = list(getattr(self.sfl, "_windows", {}))   # snapshot без лока (GIL)
                if not feeds:
                    if self._base:
                        self._base.clear()          # окон нет — базлайны не переживают затишье
                    self._stop.wait(0.3)            # вне окон цикл молчит: ноль запросов (A1/A4)
                    continue
                t0 = self._now()
                if self._cycle(feeds):
                    self._healthy_ts = self._now()
                for k in list(self._base):
                    if k not in feeds:
                        self._base.pop(k, None)
                self._maybe_stats()
                self._stop.wait(max(0.02, period - (self._now() - t0)))
            except Exception:
                log.exception("pending-detect: тик упал (слой изолирован)")
                self._stop.wait(1.0)

    def start(self):
        if not self.urls:
            log.error("PENDING-DETECT: нет допустимых провайдеров — слой не стартует "
                      "(лестница проб остаётся на полной каденсии)")
            return None
        t = threading.Thread(target=self._loop, daemon=True, name="pending-detect")
        t.start()
        return t

    def stop(self):
        self._stop.set()
