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
import random
import threading
import time

from chain.spec_fire import _label_of

log = logging.getLogger("liq.spec_fire_live")

SPEC_FIRE_MIN_REPAID_USD = float(os.environ.get("SPEC_FIRE_MIN_REPAID_USD", "500000"))
SPEC_FIRE_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_INTERVAL_SEC", "0.7"))
# ПЛОТНЫЙ РЕЖИМ (31.07: первая боевая гонка cbBTC 14:07 проиграна ДЕНЬГАМИ — победитель сидел
# в предочереди блока транзита idx 2928 с tip 0.0034, наша реакция 106мс легла блоком позже с
# tip 30): в FCFS решает не скорость и не tip, а присутствие пробы в очереди МЕЖДУ транзитом и
# спамом конкурента. Это гонка плотностей: P(win) ~ наш темп / (наш + конкурента). Хвост 2.0с
# давал ~1 пробу на блок со случайной фазой — флип 14:07 упал между пробами. Плотный режим:
# вся горячая фаза (|ret| >= порога) на DENSE_INTERVAL 0.5с (4 пробы/блок; пол send-слоя
# SPEC_FIRE_MIN_INTERVAL_SEC=0.25 совместим), джиттер ±0.15 ломает подпись расписания.
# Выравнивание фазы по границе 2с-блока НЕ строим: flashblocks каждые 200мс делают границу
# бессмысленной — окно очереди скользящее, место покупает только плотность.
# Откат без кода: SPEC_FIRE_DENSE=0 -> прежний burst/slow-каденс и кап 24.
SPEC_FIRE_DENSE = os.environ.get("SPEC_FIRE_DENSE", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_DENSE_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_DENSE_INTERVAL_SEC", "0.5"))
SPEC_FIRE_DENSE_JITTER_SEC = float(os.environ.get("SPEC_FIRE_DENSE_JITTER_SEC", "0.15"))
# Кап окна в плотном режиме: 150 × 0.5с = 75с покрытия >= p75 «первая проба -> транзит» жирных
# окон (64с, датасет 27.07). Цена худшего окна 150×$0.015 ~= $2.25 — суточный бакет $10 держит
# 4+ полных окна, WIN-cooldown (900с) рвёт серию раньше; окно и так стопится на confirmed/disarm.
SPEC_FIRE_MAX_PROBES_WINDOW = int(os.environ.get("SPEC_FIRE_MAX_PROBES_WINDOW")
                                  or (150 if SPEC_FIRE_DENSE else 24))
# КЭШ ПРОБЫ: _build на каждую пробу = 2×aggregate3 + роут — на плотном каденсе это душило бы
# ядро и RPC (эффект наблюдателя). Позиция/шары за секунды не меняются, плохой филл ревертится
# min_profit-полом контракта -> собранный calldata переиспользуем REBUILD_SEC.
# 5.0 -> 2.5 (31.07, «го» kelbic после апгрейда Шарлотты до 2 ядер): 5с = 2.5 блока жизни
# жертвы, проба могла стрелять устаревшими шарами/роутом; 2.5с = свежесть в один блок, защита
# hot path больше не требует голодания (2-е ядро). Гарды на первом окне: load>1.5 или
# деградация реакции -> откат SPEC_FIRE_REBUILD_SEC=5 (см. docs/ROLLBACKS.md).
SPEC_FIRE_REBUILD_SEC = float(os.environ.get("SPEC_FIRE_REBUILD_SEC", "2.5"))
# TIP-ГАРД: FCFS — tip НЕ покупает место (победитель 14:07 взял приз с tip 0.0034 против наших
# 30). Проба обязана остаться копеечной, даже если боевой TIP_GWEI поднимут или сработает
# каскадная ступень: жёсткий кап поверх cfg.tip_gwei.
SPEC_FIRE_TIP_CAP_GWEI = float(os.environ.get("SPEC_FIRE_TIP_CAP_GWEI", "0.1"))
# TOP-K ротация (дефолт 1 = прежний top-1): в каскад флипаются несколько жертв — K>1 чередует
# пробы по K самым жирным досягаемым целям (плотность НА ЦЕЛЬ делится на K — включать только
# когда плотность на top-1 доказала себя).
SPEC_FIRE_TOPK = max(1, int(os.environ.get("SPEC_FIRE_TOPK", "1")))
SPEC_FIRE_RECEIPT_POLL_SEC = float(os.environ.get("SPEC_FIRE_RECEIPT_POLL_SEC", "1.0"))
# КАСКАД-ДЕТЕКТ (ревью 31.07): столько армленных окон за час = каскадный день -> дёргаем
# бустер бакета в execute (тот мёртв, пока владелец не подписал SPEC_FIRE_CASCADE_GAS_USD).
SPEC_FIRE_CASCADE_WINDOWS = int(os.environ.get("SPEC_FIRE_CASCADE_WINDOWS", "4"))
SPEC_FIRE_THRESHOLD = float(os.environ.get("SPEC_FIRE_THRESHOLD", "1.0"))     # ×dev
# Predicted-HF гейт (24.07, ревью + live-данные): цена на транзите ~= текущий Binance mid,
# значит проба имеет смысл ТОЛЬКО если этот транзит способен флипнуть цель:
#   dist_to_flip <= |ret| + MARGIN_DEVS × dev   (аддитивный запас на лаг HF-кэша ~60с,
# USD-approx HF и следующий dev-шаг; НЕ мультипликативный — reach×3 на ret −3% открывал бы
# огонь по цели в 9%, которую ЭТОТ транзит не флипнет). Первый живой день: 6 окон × 10 проб
# по киту dist=8.25% на ret −0.11% — гейт отсёк бы всё ($0.59 и бакет целы).
# Арм-гейт: окно вовсе не стартует, если ближайший жир дальше ARM_MAX_DIST (нет ни петли,
# ни ончейн-фингерпринта по заведомо мёртвым окнам); в настоящий каскад HF-refresh (~60с)
# подтянет dist под потолок — последующие армы окно запустят.
# Откат без кода: SPEC_FIRE_DIST_MARGIN_DEVS=1e9 / SPEC_FIRE_ARM_MAX_DIST=1e9.
# HF цели неизвестен -> fail-open.
SPEC_FIRE_DIST_MARGIN_DEVS = float(os.environ.get("SPEC_FIRE_DIST_MARGIN_DEVS", "2.0"))
SPEC_FIRE_ARM_MAX_DIST = float(os.environ.get("SPEC_FIRE_ARM_MAX_DIST", "0.03"))
# Двухфазный каденс (по замеру 24.07: транзит приходит через 22-31с ПОСЛЕ пробития dev —
# OCR-раунд оракула; старый каденс 0.7с выжигал кап за ~7с, за 22с до транзита): первые
# BURST проб быстро (страховка от мгновенного/heartbeat-транзита), дальше медленно — кап 10
# растягивается на ~0.7+8×3.5 ~= 29с, накрывая замеренный лаг. Тюнить по новым транзитам.
SPEC_FIRE_BURST_PROBES = int(os.environ.get("SPEC_FIRE_BURST_PROBES", "2"))
# ХВОСТ 3.5 -> 2.0с и кап 10 -> 24 (27.07, shadow-датасет 871 арм/462 транзита): проба полезна
# ровно в БЛОКЕ транзита, значит ценность = доля покрытых блоков в зоне плотности. Замер
# «первая проба -> транзит» (живые окна): медиана 31с, 6-213с; окна с жиром: мед 40 p75 64с.
# 24×2.0с = ~45с покрытия ОДНОЙ пробой на КАЖДЫЙ блок (было 10×3.5 = 29с и один выстрел на
# ~1.75 блока). Цена $0.29/окно против $0.12 — суточный бакет $10 не замечает (7 окон/19д).
SPEC_FIRE_SLOW_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_SLOW_INTERVAL_SEC", "2.0"))
# ДЖИТТЕР хвоста: ровное расписание читается конкурентом как подпись слоя (тот же мотив, что
# у WIN-паузы). ±0.3с ломает периодичность почти бесплатно — покрытие блоков не страдает.
SPEC_FIRE_TAIL_JITTER_SEC = float(os.environ.get("SPEC_FIRE_TAIL_JITTER_SEC", "0.3"))
# Пауза после WIN (25.07, ревью-фингерпринт): серия «пробы -> приз» ончейн читается конкурентом
# как учебник нашего слоя. После взятого приза окна не армятся COOLDOWN сек — паттерн рвётся,
# а сам приз уже в кармане (реактивный путь паузой НЕ трогаем, он главный). 0 = выключено.
SPEC_FIRE_WIN_COOLDOWN_SEC = float(os.environ.get("SPEC_FIRE_WIN_COOLDOWN_SEC", "900"))
SPEC_FIRE_MIN_PROFIT_BPS = int(os.environ.get("SPEC_FIRE_MIN_PROFIT_BPS", "30"))
SPEC_FIRE_POLL_SEC = float(os.environ.get("SPEC_FIRE_POLL_SEC", "0.15"))
SPEC_FIRE_RECEIPT_TIMEOUT = float(os.environ.get("SPEC_FIRE_RECEIPT_TIMEOUT", "45"))


def _probe_tip_wei(tip_gwei: float) -> int:
    """Tip пробы: min(боевой, кап) — пробы НИКОГДА не наследуют каскадные ступени/эскалацию."""
    return int(min(float(tip_gwei), SPEC_FIRE_TIP_CAP_GWEI) * 1e9)


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
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._windows: dict = {}                  # feed -> stop Event
        self._armskip_ts: dict = {}               # feed -> ts последнего arm-skip-лога (троттлинг 300с)
        self._cooldown_until = 0.0                # WIN-пауза: до этого момента окна не армим
        self._arm_ts: list = []                   # ts армленных окон за час (каскад-детект)
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
            return "gone"                         # позиция разобрана (конкурент/погашение) — LOSS-стоп
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
        tip = _probe_tip_wei(self.cfg.tip_gwei)
        seq = str(getattr(self.cfg, "sequencer_rpc", "") or "")
        bcast = [seq] if (seq and seq.lower() not in ("off", "none", "0")) else None
        return send_spec_fire_probe(self.rpc, self.cfg, calldata, gas, log=log,
                                    max_fee=(fb * 2 + tip) if fb is not None else None, tip=tip,
                                    broadcast_raw_urls=bcast)

    def _receipt(self, h):
        """Ручной полл вместо wait_for_transaction_receipt: его дефолтный poll 0.1с при плотном
        залпе (6-8 сборщиков параллельно) = 60+ req/с в RPC. Раз в RECEIPT_POLL_SEC достаточно —
        Base подтверждает за 2-4с, таймаут ловит только дропы."""
        w3 = self.rpc._web3()
        deadline = self._now() + SPEC_FIRE_RECEIPT_TIMEOUT
        while self._now() < deadline:
            r = None
            try:
                r = w3.eth.get_transaction_receipt(h)
            except Exception:
                pass                              # not found / транспорт — ждём следующий полл
            if r is not None:
                gas_usd = (int(r.get("gasUsed") or 0) * int(r.get("effectiveGasPrice") or 0)
                           * float(self.cfg.eth_price_usd) / 1e18)
                # blk/idx — метрика гонки плотностей: позиция пробы в очереди против
                # транзита/победителя снимается прямо из журнала (ревью 31.07)
                return (int(r.get("status") or 0), gas_usd,
                        r.get("blockNumber"), r.get("transactionIndex"))
            self._sleep(SPEC_FIRE_RECEIPT_POLL_SEC)
        return None

    # ---- выбор жирной цели (top-1 по долгу; та же фид/направление-семантика) ----
    # max_dist (доля, напр. 0.0033): reach-гейт — пропускать цели, чей флип дальше, чем
    # достаёт текущее движение (down: цена залога должна упасть на 1-1/HF; up: долг вырасти
    # на HF-1). Применяется ТОЛЬКО в момент огня — арм окна без гейта, иначе в настоящий
    # каскад окно не запустится вовсе (на арме ret всегда мал по построению).
    def _top_fat(self, feed: str, direction: str, max_dist=None, k=1):
        """-> (top-k список [(mid, b, debt)] по долгу, hold_dist) — hold_dist = ближайшая
        отсечённая гейтом цель. Пустой список = досягаемого жира нет."""
        elig = []
        hold_dist = None
        with self.shared.lock:
            groups = dict(self.shared.groups)
            debt_by = dict(self.shared.debt_by)
            by_id = dict(self.shared.by_id)
            hf_by = dict(getattr(self.shared, "hf_by", {}) or {})
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
                if d < SPEC_FIRE_MIN_REPAID_USD:
                    continue
                if max_dist is not None:
                    hf = float(hf_by.get((mid, b), 0.0) or 0.0)
                    if hf > 1.0:                  # HF неизвестен/битый -> fail-open
                        dist = (1.0 - 1.0 / hf) if direction == "down" else (hf - 1.0)
                        if dist > max_dist:
                            if hold_dist is None or dist < hold_dist:
                                hold_dist = dist
                            continue
                elig.append((mid, b, d))
        elig.sort(key=lambda t: -t[2])
        return elig[:k], hold_dist

    def _collect(self, h, mid, borrower, debt_usd, feed=""):
        from chain.execute import specfire_gas_add, mark_recent_send, canary_invalidate_nonce
        res = (self._receipt_fn or self._receipt)(h)
        if res is None:
            canary_invalidate_nonce(self.rpc, self.cfg)   # дроп? nonce вперёд чейна -> инвалидация
            self._log("SPECFIRE probe NO-RECEIPT tx=%s (nonce инвалидирован)" % h)
            return
        status, gas_usd = res[0], res[1]
        pos = (" blk=%s idx=%s" % (res[2], res[3])) if len(res) >= 4 and res[2] is not None else ""
        specfire_gas_add(gas_usd)
        if status == 1:
            self.stats["wins"] += 1
            mark_recent_send(mid, borrower)               # жертва взята — глушим реактивный дубль
            self._cooldown_until = self._now() + SPEC_FIRE_WIN_COOLDOWN_SEC   # рвём паттерн «пробы->приз»
            if feed:                                      # приз взят — окно не жжёт кап ревертами
                with self._lock:
                    stop = self._windows.pop(feed, None)
                if stop is not None:
                    stop.set()
            log.warning("SPECFIRE WIN!!! %s/%s debt=$%.0fk tx=%s gas=$%.2f%s — предочередь взята "
                        "(окна на паузе %.0fс)",
                        mid[:10], borrower[:10], debt_usd / 1e3, h, gas_usd, pos,
                        SPEC_FIRE_WIN_COOLDOWN_SEC)
        else:
            self._log("SPECFIRE probe revert (healthy, ожидаемо) tx=%s gas=$%.3f%s" % (h, gas_usd, pos))

    def _window_loop(self, feed: str, direction: str, stop: threading.Event):
        thr = self.dev_by.get(feed, 0.005) * SPEC_FIRE_THRESHOLD
        probes = 0
        last_fire = 0.0
        last_hold_log = 0.0
        cache = {}      # (mid, b) -> (probe|None, built_ts): кэш пробы, см. SPEC_FIRE_REBUILD_SEC
        dead = set()    # LOSS-стоп: цели, разобранные конкурентом/погашенные (bs==0 на пересборке)
        while not stop.is_set() and probes < SPEC_FIRE_MAX_PROBES_WINDOW:
            r = None
            try:
                r = self.ret_fn(feed)
            except Exception:
                pass
            hot = (r is not None and ((direction == "down" and r <= -thr)
                                      or (direction == "up" and r >= thr)))
            if SPEC_FIRE_DENSE:                    # вся горячая фаза плотно (гонка плотностей, 31.07)
                _j = SPEC_FIRE_DENSE_JITTER_SEC
                iv = max(0.25, SPEC_FIRE_DENSE_INTERVAL_SEC + (random.uniform(-_j, _j) if _j > 0 else 0.0))
            elif probes < SPEC_FIRE_BURST_PROBES:
                iv = SPEC_FIRE_INTERVAL_SEC
            else:                                  # хвост с джиттером (см. SPEC_FIRE_TAIL_JITTER_SEC)
                _j = SPEC_FIRE_TAIL_JITTER_SEC
                iv = max(0.3, SPEC_FIRE_SLOW_INTERVAL_SEC + (random.uniform(-_j, _j) if _j > 0 else 0.0))
            if hot and self._now() - last_fire >= iv:
                reach = abs(r) + SPEC_FIRE_DIST_MARGIN_DEVS * self.dev_by.get(feed, 0.005)
                tgts, hold_dist = self._top_fat(feed, direction, max_dist=reach, k=SPEC_FIRE_TOPK)
                if dead:
                    tgts = [t for t in tgts if (t[0], t[1]) not in dead]
                if not tgts:
                    if hold_dist is None:
                        break                              # жир ушёл из набора — окно нам не нужно
                    if self._now() - last_hold_log >= 10.0:   # жир есть, но флип вне досягаемости
                        last_hold_log = self._now()           # движения — держим окно, НЕ жжём бакет
                        self._log("SPECFIRE hold feed=%s: жир вне досягаемости (ближ. dist=%.2f%% > reach=%.2f%%)" % (
                            feed, hold_dist * 100.0, reach * 100.0))
                    stop.wait(SPEC_FIRE_POLL_SEC)
                    continue
                mid, b, d = tgts[probes % len(tgts)]       # top-K ротация (K=1 -> прежний top-1)
                try:
                    probe, built_ts = cache.get((mid, b), (None, 0.0))
                    if probe is None or self._now() - built_ts >= SPEC_FIRE_REBUILD_SEC:
                        probe = (self._build_fn or self._build)(mid, b, d)
                        if probe == "gone":       # LOSS-стоп (ревью 31.07): жертву разобрали —
                            dead.add((mid, b))    # кап не догорает ревертами по мёртвой цели
                            cache.pop((mid, b), None)
                            self._log("SPECFIRE loss-stop feed=%s %s/%s: позиция разобрана "
                                      "(конкурент/погашение) — цель снята" % (feed, mid[:10], b[:10]))
                            continue
                        cache[(mid, b)] = (probe, self._now())
                    if probe is not None:
                        h = (self._send_fn or self._send)(probe["calldata"], probe["gas"])
                        if h is not None:
                            probes += 1
                            self.stats["probes"] += 1
                            last_fire = self._now()
                            self._log("SPECFIRE probe #%d feed=%s %s/%s debt=$%.0fk ret=%.4f%% tx=%s" % (
                                probes, feed, mid[:10], b[:10], d / 1e3, (r or 0) * 100.0, h))
                            threading.Thread(target=self._collect, args=(h, mid, b, d, feed),
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
                if self._now() < self._cooldown_until:     # WIN-пауза: приз взят, паттерн не палим
                    return None
                best, hold_dist = self._top_fat(feed, direction, max_dist=SPEC_FIRE_ARM_MAX_DIST)
                if not best:
                    if hold_dist is not None:              # жир есть, но заведомо вне окна — не палим
                        _n = self._now()                   # ни петлю, ни ончейн-паттерн
                        if _n - self._armskip_ts.get(feed, -1e9) >= 300.0:
                            self._armskip_ts[feed] = _n
                            self._log("SPECFIRE arm-skip feed=%s: ближ. жир dist=%.2f%% > потолок %.1f%%" % (
                                feed, hold_dist * 100.0, SPEC_FIRE_ARM_MAX_DIST * 100.0))
                    return None                            # окна без (досягаемого) жира не запускают петлю
                with self._lock:
                    old = self._windows.pop(feed, None)
                    if old is not None:
                        old.set()
                    stop = threading.Event()
                    self._windows[feed] = stop
                    self.stats["windows_armed"] += 1
                _n = self._now()                           # каскад-детект: спам армов за час
                self._arm_ts = [t for t in self._arm_ts if _n - t < 3600.0] + [_n]
                if len(self._arm_ts) >= SPEC_FIRE_CASCADE_WINDOWS:
                    from chain.execute import specfire_cascade_boost
                    if specfire_cascade_boost() and _n - self._armskip_ts.get("_boost", -1e9) >= 300.0:
                        self._armskip_ts["_boost"] = _n
                        self._log("SPECFIRE каскад-детект: %d окон/час — бакет поднят до каскадного "
                                  "(TTL 1ч, продлевается армами)" % len(self._arm_ts))
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
