"""SPEC-канарейка: замер достижимости ПРЕДОЧЕРЕДИ (delta 0..5 от транзита — 11% призов,
победители-спекулянты) БЕЗ ликвидаций и риска. На PREDICT event=arm шлём копеечные self-tx
(боевой send-путь: fb-fee + SEND-3) каждые SPEC_CANARY_INTERVAL_SEC до пуша/отбоя; на
event=confirmed находим блок транзита (AnswerUpdated на агрегаторе) и меряем, на какой idx
сел спам-канар, попавший в блок пуша. delta<=5 => предочередь достижима нашей трассой =>
боевой спекулятивный огонь имеет смысл. Гейты: PREDICT_SPEC_CANARY (OFF), кап проб/окно,
кап окон кампании, ОБЩИЙ канареечный газ-кап (send_canary spec-режим). Инжектируемые швы
(send_fn/receipt_fn/logs_fn/now/sleep) — офлайн-юниты без сети."""
from __future__ import annotations
import os
import threading
import time
import logging

log = logging.getLogger("liq.spec_canary")

SPEC_INTERVAL_SEC = float(os.environ.get("SPEC_CANARY_INTERVAL_SEC", "2.0"))    # ~= block time: не пропустить блок пуша
SPEC_MAX_PER_WINDOW = int(os.environ.get("SPEC_CANARY_MAX_PER_WINDOW", "90"))    # ~180с окна при 2с; длинные lead частично
SPEC_MAX_WINDOWS = int(os.environ.get("SPEC_CANARY_MAX_WINDOWS", "60"))
SPEC_MAX_PROBES = int(os.environ.get("SPEC_CANARY_MAX_PROBES", "1500"))
SPEC_WINDOW_CAP_SEC = float(os.environ.get("SPEC_CANARY_WINDOW_CAP_SEC", "300"))
SPEC_LOGS_SPAN = int(os.environ.get("SPEC_CANARY_LOGS_SPAN", "20"))              # блоков назад для AnswerUpdated

_ANSWER_UPDATED_SIG = None


def _sig():
    global _ANSWER_UPDATED_SIG
    if _ANSWER_UPDATED_SIG is None:
        from eth_utils import keccak
        _ANSWER_UPDATED_SIG = "0x" + keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
    return _ANSWER_UPDATED_SIG


class SpecCanary:
    """Одно окно на символ одновременно; события драйвера (on_event) -> старт/стоп/замер."""

    def __init__(self, rpc, cfg, proxies: dict,
                 send_fn=None, receipt_fn=None, logs_fn=None, agg_fn=None, bn_fn=None,
                 inflight_fn=None, gas_fn=None, label2sym=None,
                 now=time.monotonic, sleep=time.sleep):
        self.rpc, self.cfg = rpc, cfg
        self.proxies = {k.upper(): v for k, v in (proxies or {}).items()}
        # события драйвера несут feed=LABEL ("BTC"), а proxies ключуются символом ("BTCUSDT").
        # label2sym из FEEDS_BASE переводит; дефолт строится лениво. Также кладём label прямо в
        # proxies (label->proxy), чтобы _transmit_of работал по любому ключу.
        if label2sym is None:
            try:
                from chain.predict import FEEDS_BASE
                label2sym = {row["label"].upper(): sym.upper() for sym, row in FEEDS_BASE.items()}
            except Exception:
                label2sym = {}
        self._label2sym = label2sym
        for lbl, sym in label2sym.items():
            if sym in self.proxies and lbl not in self.proxies:
                self.proxies[lbl] = self.proxies[sym]      # label -> proxy (алиас)
        self._now, self._sleep = now, sleep
        self._lock = threading.Lock()
        self._windows: dict = {}          # sym -> {stop: Event, hashes: [(hash, mono)], started: mono}
        self.windows_done = 0
        # швы (None = боевые реализации, лениво)
        self._send_fn = send_fn
        self._receipt_fn = receipt_fn
        self._logs_fn = logs_fn
        self._agg_fn = agg_fn
        self._bn_fn = bn_fn
        self._inflight_fn = inflight_fn
        self._gas_fn = gas_fn

    # ---- боевые реализации швов ----
    def _send(self):
        if self._send_fn is not None:
            return self._send_fn()
        from chain.execute import send_canary
        from chain.hotpath import _fresh_basefee
        fb = _fresh_basefee()
        # Ш30: тот же tip, что у боевого пути (см. hotpath). Прибор обязан мерить ТОТ объект.
        from chain.hotpath import dynamic_tip_gwei as _dyn
        tip = int(_dyn(self.cfg, 0.0) * 1e9)
        seq = str(getattr(self.cfg, "sequencer_rpc", "") or "")
        bcast = [seq] if (seq and seq.lower() not in ("off", "none", "0")) else None
        return send_canary(self.rpc, self.cfg, log,
                           max_fee=(fb * 2 + tip) if fb is not None else None, tip=tip,
                           broadcast_raw_urls=bcast,
                           spec_interval_sec=SPEC_INTERVAL_SEC, spec_max_probes=SPEC_MAX_PROBES)

    def _receipt(self, h):
        if self._receipt_fn is not None:
            return self._receipt_fn(h)
        try:
            r = self.rpc._web3().eth.get_transaction_receipt(h)
            return (int(r["blockNumber"]), int(r["transactionIndex"]),
                    int(r.get("gasUsed") or 0) * int(r.get("effectiveGasPrice") or 0))
        except Exception:
            return None

    def _transmit_of(self, sym):
        """(block, idx) последнего AnswerUpdated фида sym вокруг текущей головы, None если не найден."""
        if self._logs_fn is not None:
            return self._logs_fn(sym)
        try:
            from web3 import Web3
            from chain.rpc import get_logs_fb
            w3 = self.rpc._web3()
            proxy = self.proxies.get(sym)
            if not proxy:
                return None
            agg = (self._agg_fn(sym) if self._agg_fn is not None else
                   w3.eth.contract(address=Web3.to_checksum_address(proxy),
                                   abi=[{"name": "aggregator", "type": "function", "inputs": [],
                                         "outputs": [{"type": "address"}],
                                         "stateMutability": "view"}]).functions.aggregator().call())
            bn = self._bn_fn() if self._bn_fn is not None else int(w3.eth.block_number)
            logs = get_logs_fb(w3, {"fromBlock": bn - SPEC_LOGS_SPAN, "toBlock": bn,
                                    "address": Web3.to_checksum_address(agg), "topics": [_sig()]})
            if not logs:
                return None
            last = logs[-1]
            return (int(last["blockNumber"]), int(last["transactionIndex"]))
        except Exception as e:
            log.info("DIAG spec-canary transmit-resolve fail feed=%s err=%s", sym, str(e)[:60])
            return None

    def _inflight(self):
        if self._inflight_fn is not None:
            return self._inflight_fn()
        from chain.execute import current_inflight
        return current_inflight()

    def _record_gas(self, wei_cost):
        usd = wei_cost * getattr(self.cfg, "eth_price_usd", 1730.0) / 1e18
        if self._gas_fn is not None:
            self._gas_fn(usd); return
        from chain.execute import canary_record_gas
        canary_record_gas(usd)

    # ---- события драйвера ----
    def handle_event(self, ev: dict):
        kind, sym = ev.get("event"), str(ev.get("feed") or "").upper()
        if not sym:
            return
        if kind == "arm":
            self._start_window(sym)
        elif kind == "confirmed":
            self._finish_window(sym, pushed=True)
        elif kind in ("falsepos", "disarm", "stale"):
            self._finish_window(sym, pushed=False)

    def _start_window(self, sym):
        with self._lock:
            # ОДНО активное окно за раз: глобальный spec-интервал (2с) всё равно сериализует send'ы
            # через один nonce -> два окна голодали бы друг друга; одно окно = чистый per-feed каденс.
            if self._windows or self.windows_done >= SPEC_MAX_WINDOWS:
                return
            w = {"stop": threading.Event(), "hashes": [], "started": self._now()}
            self._windows[sym] = w
        threading.Thread(target=self._window_loop, args=(sym, w), daemon=True).start()
        log.info("DIAG spec-canary window-start feed=%s interval=%.1fs cap=%d", sym,
                 SPEC_INTERVAL_SEC, SPEC_MAX_PER_WINDOW)

    def _window_loop(self, sym, w):
        # шлём пробы каждые SPEC_INTERVAL_SEC пока: не пришёл finish-event (stop), не выбран кап проб,
        # не истёк window-cap. Когда пробить больше нельзя, но finish ещё не пришёл — держим слот до
        # дедлайна, затем САМОЗАКРЫТИЕ (иначе одно окно заняло бы single-slot навечно, если пуша нет).
        deadline = w["started"] + SPEC_WINDOW_CAP_SEC
        while not w["stop"].is_set():
            now = self._now()
            if now < deadline and len(w["hashes"]) < SPEC_MAX_PER_WINDOW and self._inflight() == 0:
                try:
                    h = self._send()
                except Exception:
                    h = None
                    log.info("DIAG spec-canary send fail feed=%s", sym)
                if h:
                    w["hashes"].append((h, now))
            if now >= deadline and not w["stop"].is_set():
                with self._lock:
                    gone = self._windows.pop(sym, None) is not None
                if gone:
                    self.windows_done += 1
                    log.info("DIAG spec-canary window-timeout feed=%s probes=%d (пуша не дождались %ds)",
                             sym, len(w["hashes"]), int(SPEC_WINDOW_CAP_SEC))
                return
            w["stop"].wait(timeout=SPEC_INTERVAL_SEC)

    def _finish_window(self, sym, pushed: bool):
        with self._lock:
            w = self._windows.pop(sym, None)
        if w is None:
            return
        w["stop"].set()
        self.windows_done += 1
        if pushed and w["hashes"]:
            threading.Thread(target=self._measure, args=(sym, list(w["hashes"])), daemon=True).start()
        else:
            log.info("DIAG spec-canary window-end feed=%s pushed=%s probes=%d (замера нет)",
                     sym, pushed, len(w["hashes"]))

    def _measure(self, sym, hashes):
        """Bg: подождать пропагацию -> блок/idx транзита -> receipts спама -> SPEC-DELTA/NEAR."""
        self._sleep(3.0)
        tr = self._transmit_of(sym)
        rcpts = []
        missing = 0
        for h, _ts in hashes[-40:]:
            r = self._receipt(h)
            if r is None:
                self._sleep(2.0)
                r = self._receipt(h)
            if r is None:
                missing += 1
                continue
            rcpts.append(r)
            self._record_gas(r[2])
        if tr is None:
            log.info("DIAG spec-canary window-end feed=%s pushed=True probes=%d transmit=UNRESOLVED "
                     "landed=%d missing=%d", sym, len(hashes), len(rcpts), missing)
            return
        tblk, tidx = tr
        in_block = [r for r in rcpts if r[0] == tblk]
        if in_block:
            best = min(r[1] for r in in_block)
            log.info("DIAG spec-canary DELTA feed=%s delta=%d canary_idx=%d transmit_idx=%d "
                     "block=%d probes=%d | ПРЕДОЧЕРЕДЬ: delta<=5 => достижима",
                     sym, best - tidx, best, tidx, tblk, len(hashes))
        else:
            after = [r for r in rcpts if r[0] > tblk]
            near = min(after, key=lambda r: (r[0], r[1])) if after else None
            log.info("DIAG spec-canary NEAR feed=%s transmit_blk=%d нет канара в блоке пуша; "
                     "ближайший blk=%s idx=%s (каденс %.1fs > кадра) probes=%d",
                     sym, tblk, near[0] if near else "-", near[1] if near else "-",
                     SPEC_INTERVAL_SEC, len(hashes))
        if missing:
            from chain.execute import canary_invalidate_nonce
            canary_invalidate_nonce(self.rpc, self.cfg)
            log.info("DIAG spec-canary missing=%d receipts feed=%s -> nonce invalidated (safety)",
                     missing, sym)
