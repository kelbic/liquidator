"""SPEC-FIRE LIVE (Stage B, 24.07): боевые пробы-ликвидации по ЖИРНЫМ целям в последние
секунды predict-окна — попытка взять предочередь (11% призов, победители-спекулянты idx+2).

Гейт снят данными (см. STATE 24.07): бэктест каскада 24-25.06 (87% ликвидаций при армленном
окне, lead med 67с) + live spec-canary DELTA=4/3 (предочередь достижима). Дизайн:
- ОКНО: arm-событие с целями >= SPEC_FIRE_MIN_REPAID_USD ($500k, директива kelbic).
- ОГОНЬ: только когда |ret| >= dev × SPEC_FIRE_THRESHOLD (1.0) — транзит неминуем в секундах
  (оракул триггерится на dev); до порога НЕ стреляем (там lead минуты — пробы в пустоту).
- ПРОБА: настоящий liquidate-calldata top-1 жирной цели: полный repaid, своп из PREARM-кэша
  (мега: Kyber-агрегат, обрыв пула) либо direct-роут (0 RTT); minProfit-floor аналитический
  (SPEC_FIRE_MIN_PROFIT_BPS от repaid) — контракт ревертит плохой филл. До флипа Morpho
  ревертит 'healthy' (~$0.01-0.02); проба в блоке транзита ПОСЛЕ него = WIN полным призом.
- БЕЗОПАСНОСТЬ: send через send_spec_fire_probe (канареечная nonce-машинерия под общим
  _SEND_LOCK; _INFLIGHT>0 -> пробы молчат — боевая tx главнее); пробы НЕ трогают
  _RECENT_SENDS (не глушат реактивный выстрел); WIN маркирует дедуп отдельно; свой
  газ-бакет SPEC_FIRE_MAX_GAS_USD; капы проб на окно; receipts собираются фоном
  (kill-switch-слепоты к газу нет — газ букается в бакет по receipt).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from chain.spec_fire import _label_of

log = logging.getLogger("liq.spec_fire_live")

SPEC_FIRE_MIN_REPAID_USD = float(os.environ.get("SPEC_FIRE_MIN_REPAID_USD", "500000"))
SPEC_FIRE_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_INTERVAL_SEC", "0.7"))
SPEC_FIRE_MAX_PROBES_WINDOW = int(os.environ.get("SPEC_FIRE_MAX_PROBES_WINDOW", "10"))
SPEC_FIRE_THRESHOLD = float(os.environ.get("SPEC_FIRE_THRESHOLD", "1.0"))     # ×dev
SPEC_FIRE_MIN_PROFIT_BPS = int(os.environ.get("SPEC_FIRE_MIN_PROFIT_BPS", "30"))
SPEC_FIRE_POLL_SEC = float(os.environ.get("SPEC_FIRE_POLL_SEC", "0.15"))
SPEC_FIRE_RECEIPT_TIMEOUT = float(os.environ.get("SPEC_FIRE_RECEIPT_TIMEOUT", "45"))


class SpecFireLive:
    """Потребитель событий PredictDriver + fire-петля на окно. Все внешние вызовы — через швы."""

    def __init__(self, rpc, cfg, shared, ret_fn, dev_by: dict,
                 build_fn=None, send_fn=None, receipt_fn=None, log_fn=None,
                 now=time.monotonic, sleep=None):
        self.rpc, self.cfg, self.shared = rpc, cfg, shared
        self.ret_fn = ret_fn                      # label -> текущий return к якорю (None = нет данных)
        self.dev_by = {k.upper(): float(v) for k, v in dev_by.items()}
        self._build_fn = build_fn                 # шов: (mid, borrower, debt_usd) -> {calldata, gas}|None
        self._send_fn = send_fn                   # шов: (calldata, gas) -> hash|None
        self._receipt_fn = receipt_fn             # шов: (hash) -> (status, gas_usd)|None
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._now = now
        self._lock = threading.Lock()
        self._windows: dict = {}                  # feed -> stop Event
        self.stats = {"windows_armed": 0, "probes": 0, "wins": 0}

    # ---- боевые швы ----
    def _build(self, mid, borrower, debt_usd):
        from chain.multicall import (aggregate3, encode_id_to_market_params_call,
            decode_id_to_market_params, encode_market_call, decode_market,
            encode_position_call, decode_position, encode_price_call, decode_price)
        from chain.simulate import to_assets_up
        from strategy.pnl import lif_from_lltv
        from chain.execute import (expected_seized, direct_swap, encode_liquidate,
                                   _MORPHO_BLUE)
        from chain.prearm import prearm_get
        m32 = self.rpc.to_bytes32(mid)
        r1 = aggregate3(self.rpc, [(_MORPHO_BLUE, encode_id_to_market_params_call(m32)),
                                   (_MORPHO_BLUE, encode_market_call(m32)),
                                   (_MORPHO_BLUE, encode_position_call(m32, borrower))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
        mk = decode_market(r1[1][1]); tba, tbs = mk[2], mk[3]
        bs, _ = decode_position(r1[2][1])
        if bs == 0:
            return None
        price = decode_price(aggregate3(self.rpc, [(oracle, encode_price_call())])[0][1])
        repaid_shares = int(bs)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
        if seized == 0:
            return None
        liq = self.cfg.liquidator_address
        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle,
              "irm": irm, "lltv": lltv_wad}
        fl_max = float(getattr(self.cfg, "fast_lane_max_repaid_usd", 500000.0) or 500000.0)
        swap = None
        if debt_usd >= fl_max:                    # мега: обрыв одиночного пула -> сперва PREARM (Kyber)
            hit = prearm_get(coll, loan, int(seized))
            if hit is not None:
                swap = hit["swap"]
        if swap is None:
            swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
        if swap is None:
            hit = prearm_get(coll, loan, int(seized))
            swap = hit["swap"] if hit is not None else None
        if swap is None:
            return None                           # ни роута, ни прогретой квоты — пробу НЕ строим
        min_profit = int(repaid_assets) * SPEC_FIRE_MIN_PROFIT_BPS // 10000
        calldata = encode_liquidate(mp, borrower, repaid_shares, swap["router"],
                                    swap["calldata"], min_profit)
        return {"calldata": calldata, "gas": int(self.cfg.gas_limit_est * 125 // 100)}

    def _send(self, calldata, gas):
        from chain.execute import send_spec_fire_probe
        from chain.hotpath import _fresh_basefee
        fb = _fresh_basefee()
        tip = int(self.cfg.tip_gwei * 1e9)
        seq = str(getattr(self.cfg, "sequencer_rpc", "") or "")
        bcast = [seq] if (seq and seq.lower() not in ("off", "none", "0")) else None
        return send_spec_fire_probe(self.rpc, self.cfg, calldata, gas, log=log,
                                    max_fee=(fb * 2 + tip) if fb is not None else None, tip=tip,
                                    broadcast_raw_urls=bcast)

    def _receipt(self, h):
        try:
            r = self.rpc._web3().eth.wait_for_transaction_receipt(h, timeout=SPEC_FIRE_RECEIPT_TIMEOUT)
            gas_usd = (int(r.get("gasUsed") or 0) * int(r.get("effectiveGasPrice") or 0)
                       * float(self.cfg.eth_price_usd) / 1e18)
            return int(r.get("status") or 0), gas_usd
        except Exception:
            return None

    # ---- выбор жирной цели (top-1 по долгу; та же фид/направление-семантика) ----
    def _top_fat(self, feed: str, direction: str):
        best = None
        with self.shared.lock:
            groups = dict(self.shared.groups)
            debt_by = dict(self.shared.debt_by)
            by_id = dict(self.shared.by_id)
        for mid, borrowers in groups.items():
            m = by_id.get(mid)
            if m is None:
                continue
            cl = _label_of(getattr(m, "collateral_token", ""))
            ll = _label_of(getattr(m, "loan_token", ""))
            if cl == ll or not ((direction == "down" and cl == feed) or (direction == "up" and ll == feed)):
                continue
            for b in borrowers:
                d = float(debt_by.get((mid, b), 0.0) or 0.0)
                if d >= SPEC_FIRE_MIN_REPAID_USD and (best is None or d > best[2]):
                    best = (mid, b, d)
        return best

    def _collect(self, h, mid, borrower, debt_usd):
        from chain.execute import specfire_gas_add, mark_recent_send, canary_invalidate_nonce
        res = (self._receipt_fn or self._receipt)(h)
        if res is None:
            canary_invalidate_nonce(self.rpc, self.cfg)   # дроп? nonce вперёд чейна -> инвалидация
            self._log("SPECFIRE probe NO-RECEIPT tx=%s (nonce инвалидирован)" % h)
            return
        status, gas_usd = res
        specfire_gas_add(gas_usd)
        if status == 1:
            self.stats["wins"] += 1
            mark_recent_send(mid, borrower)               # жертва взята — глушим реактивный дубль
            log.warning("SPECFIRE WIN!!! %s/%s debt=$%.0fk tx=%s gas=$%.2f — предочередь взята",
                        mid[:10], borrower[:10], debt_usd / 1e3, h, gas_usd)
        else:
            self._log("SPECFIRE probe revert (healthy, ожидаемо) tx=%s gas=$%.3f" % (h, gas_usd))

    def _window_loop(self, feed: str, direction: str, stop: threading.Event):
        thr = self.dev_by.get(feed, 0.005) * SPEC_FIRE_THRESHOLD
        probes = 0
        last_fire = 0.0
        while not stop.is_set() and probes < SPEC_FIRE_MAX_PROBES_WINDOW:
            r = None
            try:
                r = self.ret_fn(feed)
            except Exception:
                pass
            hot = (r is not None and ((direction == "down" and r <= -thr)
                                      or (direction == "up" and r >= thr)))
            if hot and self._now() - last_fire >= SPEC_FIRE_INTERVAL_SEC:
                tgt = self._top_fat(feed, direction)
                if tgt is None:
                    break                                  # жир ушёл из набора — окно нам не нужно
                mid, b, d = tgt
                try:
                    probe = (self._build_fn or self._build)(mid, b, d)
                    if probe is not None:
                        h = (self._send_fn or self._send)(probe["calldata"], probe["gas"])
                        if h is not None:
                            probes += 1
                            self.stats["probes"] += 1
                            last_fire = self._now()
                            self._log("SPECFIRE probe #%d feed=%s %s/%s debt=$%.0fk ret=%.4f%% tx=%s" % (
                                probes, feed, mid[:10], b[:10], d / 1e3, (r or 0) * 100.0, h))
                            threading.Thread(target=self._collect, args=(h, mid, b, d),
                                             daemon=True).start()
                    else:
                        self._log("SPECFIRE probe skip feed=%s %s/%s: нет свопа (роут/prearm)" % (
                            feed, mid[:10], b[:10]))
                        stop.wait(2.0)                     # не молотить чтениями без свопа
                except Exception as e:
                    self._log("SPECFIRE probe fail feed=%s: %s" % (feed, type(e).__name__))
                    stop.wait(1.0)
            stop.wait(SPEC_FIRE_POLL_SEC)

    def handle_event(self, ev: dict):
        try:
            kind = ev.get("event")
            feed = (ev.get("feed") or "").upper()
            if kind == "arm":
                direction = ev.get("dir") or "down"
                if self._top_fat(feed, direction) is None:
                    return None                            # окна без жира не запускают петлю вовсе
                with self._lock:
                    old = self._windows.pop(feed, None)
                    if old is not None:
                        old.set()
                    stop = threading.Event()
                    self._windows[feed] = stop
                    self.stats["windows_armed"] += 1
                self._log("SPECFIRE LIVE window feed=%s dir=%s — жир на прицеле, огонь при |ret|>=%.3f%%" % (
                    feed, direction, self.dev_by.get(feed, 0.005) * SPEC_FIRE_THRESHOLD * 100.0))
                threading.Thread(target=self._window_loop, args=(feed, direction, stop),
                                 daemon=True, name="specfire-%s" % feed).start()
            elif kind in ("confirmed", "push", "disarm", "falsepos", "stale"):
                with self._lock:
                    stop = self._windows.pop(feed, None)
                if stop is not None:
                    stop.set()
        except Exception:
            log.exception("spec-fire live: событие %s упало (слой изолирован)", ev.get("event"))
        return None
