"""Юниты SPEC-FIRE LIVE (Stage B): порог |ret|>=dev, каденс, капы, WIN-маркировка, изоляция. Без сети."""
import sys
import threading
import time
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.spec_fire_live as sfl_mod
# Стенд различает «жир» ($1.5M) и «пыль» ($90k) ГЕЙТОМ порога — пин явный: тест проверяет
# сам механизм отсечки, а не дефолт модуля (дефолт-призрак $500k убран 19.08 по слову kelbic).
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = 500_000.0
import chain.execute as ex
from chain.spec_fire_live import SpecFireLive
import chain.spec_fire_live as _sfl_v1
_sfl_v1.SPEC_FIRE_V1_MAX_PUSHES = 1   # В1: этим стендам нужна семантика «пуш закрывает»

# файл тестирует ДО-owed семантику (каденс/кэш/гейты, disarm глушит окно безусловно);
# peak-owed (02.08) делает disarm sticky при горячем ret — его контракты в test_peak_owed.py
sfl_mod.SPEC_FIRE_PEAK_OWED = False

LOGS = []


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)


shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT", "0xdust"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0, ("0xbtc", "0xdust"): 90_000.0},
    by_id={"0xbtc": mk("cbBTC", "USDC")},
)
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                            tip_gwei=0.05, sequencer_rpc="https://seq", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)

RET = {"v": 0.0}
sent, receipts = [], []


def build_fn(mid, b, d):
    return {"calldata": "0xdeadbeef", "gas": 625_000}


def send_fn(calldata, gas):
    sent.append((calldata, gas))
    return "0xhash%d" % len(sent)


def receipt_fn(h):
    receipts.append(h)
    return (0, 0.011)     # revert 'healthy'


def make(ret_v=RET, **kw):
    kw.setdefault("build_fn", build_fn)
    kw.setdefault("send_fn", send_fn)
    kw.setdefault("receipt_fn", receipt_fn)
    kw.setdefault("log_fn", LOGS.append)
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared,
                        ret_fn=lambda lbl: ret_v["v"], dev_by={"BTC": 0.0010}, **kw)


# 1) arm без жира -> петля не стартует
sf = make()
empty_shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
sf2 = SpecFireLive(rpc=None, cfg=cfg, shared=empty_shared, ret_fn=lambda l: -1.0,
                   dev_by={"BTC": 0.001}, build_fn=build_fn, send_fn=send_fn,
                   receipt_fn=receipt_fn, log_fn=LOGS.append)
sf2.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.2)
assert sf2.stats["windows_armed"] == 0 and not sent
print("UNIT_OK: окно без жира не запускает петлю")

# 2) ниже порога (|ret| < dev) — огня НЕТ; выше — пробы идут ПЛОТНО (дефолт DENSE 0.5±0.15)
sent.clear(); RET["v"] = -0.0005          # 0.05% < dev 0.10%
sf.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.5)
assert sf.stats["windows_armed"] == 1 and not sent, sent
RET["v"] = -0.0011                        # пробили dev вниз
time.sleep(1.2)
sf.handle_event({"event": "push", "feed": "BTC"})
n1 = len(sent)
# EV-плотность (06.08): жир $1.5M в фикстуре -> каденс 0.12±0.04, квант петли 0.15с -> ~8 проб
assert 5 <= n1 <= 12, sent
print("UNIT_OK: до dev тишина, после dev EV-плотные пробы (n=%d за 1.2с)" % n1)

# 3) push стопит окно; новых проб нет
time.sleep(0.6)
assert len(sent) == n1
print("UNIT_OK: push останавливает огонь")

# 3b) EV-плотность off -> штатный DENSE-каденс 0.5±0.15 (за ~1.2с 2-3 пробы)
sfl_mod.SPEC_FIRE_EV_DENSITY = False
sent.clear(); RET["v"] = -0.0005
sf_ev = make()
sf_ev.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.3)
RET["v"] = -0.0011
time.sleep(1.2)
sf_ev.handle_event({"event": "push", "feed": "BTC"})
n_off = len(sent)
assert 1 <= n_off <= 4, sent
sfl_mod.SPEC_FIRE_EV_DENSITY = True
print("UNIT_OK: откат SPEC_FIRE_EV_DENSITY=0 -> штатный каденс (n=%d за 1.2с)" % n_off)

# 4) dir=up не стреляет на ret<0 (направление сверяется)
sent.clear(); RET["v"] = -0.002
sf.handle_event({"event": "arm", "feed": "BTC", "dir": "up"})
time.sleep(0.5)
sf.handle_event({"event": "disarm", "feed": "BTC"})
assert not sent
print("UNIT_OK: направление окна сверяется со знаком ret")

# 5) кап проб на окно (плотный режим, пол интервала 0.25с; EV-режим гасим — его кап в 5b)
sent.clear(); RET["v"] = -0.005
old_cap = sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW
sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW = 2
old_div = sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC
old_dj = sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC
sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC = 0.05      # пол max(0.25, ...) удержит 0.25
sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC = 0.0
sfl_mod.SPEC_FIRE_EV_DENSITY = False
sf3 = make()
sf3.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.8)
sf3.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) == 2, sent
sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW = old_cap
sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC = old_div
sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC = old_dj
sfl_mod.SPEC_FIRE_EV_DENSITY = True
print("UNIT_OK: кап проб/окно соблюдён (и пол интервала 0.25с в плотном режиме)")

# 5b) EV-кап (06.08): жир в окне -> кап = SPEC_FIRE_FAT_CAP, каденс жирный
sent.clear(); RET["v"] = -0.005
old_fcap = sfl_mod.SPEC_FIRE_FAT_CAP
sfl_mod.SPEC_FIRE_FAT_CAP = 3
sf3b = make()
sf3b.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.8)
sf3b.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) == 3, sent
sfl_mod.SPEC_FIRE_FAT_CAP = old_fcap
print("UNIT_OK: EV-кап жирного окна соблюдён (FAT_CAP)")

# 6) WIN: receipt status=1 -> _RECENT_SENDS маркирован; revert -> НЕ маркирован
ex._RECENT_SENDS.clear()
sf4 = make(receipt_fn=lambda h: (1, 0.02))
sf4._collect("0xh", "0xbtc", "0xFAT", 1_500_000.0)
assert ("0xbtc", "0xFAT") in ex._RECENT_SENDS and sf4.stats["wins"] == 1
ex._RECENT_SENDS.clear()
sf5 = make(receipt_fn=lambda h: (0, 0.011))
sf5._collect("0xh2", "0xbtc", "0xFAT", 1_500_000.0)
assert ("0xbtc", "0xFAT") not in ex._RECENT_SENDS and sf5.stats["wins"] == 0
print("UNIT_OK: WIN маркирует дедуп, revert — нет (реактивный путь не глушится)")

# 7a) АРМ-гейт: жир заведомо вне окна (dist > потолок 3%) -> окно вовсе не стартует
def mk_shared(hf):
    return types.SimpleNamespace(
        lock=threading.Lock(),
        groups={"0xbtc": ["0xFAT"]},
        debt_by={("0xbtc", "0xFAT"): 1_500_000.0},
        hf_by={("0xbtc", "0xFAT"): hf},
        by_id={"0xbtc": mk("cbBTC", "USDC")},
    )
sent.clear(); LOGS.clear(); RET["v"] = -0.05
sf6 = SpecFireLive(rpc=None, cfg=cfg, shared=mk_shared(1.0899), ret_fn=lambda l: RET["v"],
                   dev_by={"BTC": 0.0010}, build_fn=build_fn, send_fn=send_fn,
                   receipt_fn=receipt_fn, log_fn=LOGS.append)   # dist=8.25% (живой кит 24.07)
sf6.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.3)
assert sf6.stats["windows_armed"] == 0 and not sent
assert any("arm-skip" in m and "8.25" in m for m in LOGS), LOGS
print("UNIT_OK: арм-гейт — мёртвое окно не стартует (arm-skip, ни петли, ни фингерпринта)")

# 7b) FIRE-гейт (predicted-HF): dist 2% армится (< потолка), малый ret -> hold;
#     ret с досягаемостью |ret|+2×dev >= dist -> огонь
sent.clear(); LOGS.clear(); RET["v"] = -0.0011    # достягаемость 0.11%+0.20% = 0.31% < 2%
# 27.08: мир этой фикстуры — толчки ~2% (ниже RET=-0.019). Кольцо масштаба обязано описывать ТОТ
# ЖЕ мир, иначе новый гейт досягаемости (p99 толчков фида) срежет цель на 2% ДО FIRE-гейта, и
# тест 7b будет проверять не то, что заявляет. Боевой замер BTC (max 0.226%) здесь неуместен —
# он описывает боевой рынок, а не эту фикстуру.
import chain.push_scale as _ps_live
_ps_live._RINGS.clear()
# Потолок CAP_PCT=1.5% — БОЕВОЙ предохранитель (максимум замеренного толчка 0.601%), но мир этой
# фикстуры устроен вокруг движения 2%, поэтому здесь потолок поднимается ЯВНО и локально.
# Иначе гейт срежет цель на 2.00% раньше FIRE-гейта, и 7b проверит не то, что заявляет.
_ps_live_cap = _ps_live.CAP_PCT
_ps_live.CAP_PCT = 5.0
for _i in range(_ps_live.MIN_N + 2):
    _ps_live.note_push("BTC", 2.10)
sf7 = SpecFireLive(rpc=None, cfg=cfg, shared=mk_shared(1.0204), ret_fn=lambda l: RET["v"],
                   dev_by={"BTC": 0.0010}, build_fn=build_fn, send_fn=send_fn,
                   receipt_fn=receipt_fn, log_fn=LOGS.append)   # dist=2.00%
sf7.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.6)
assert sf7.stats["windows_armed"] == 1 and not sent, sent
assert any("hold" in m and "2.00" in m for m in LOGS), LOGS
_ps_live.CAP_PCT = _ps_live_cap                   # потолок ВЕРНУТЬ: глобал протекал на процесс
RET["v"] = -0.019                                 # 1.9%+0.2% = 2.1% >= 2.00% — этот транзит флипает
time.sleep(1.0)
sf7.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) >= 1, "в пределах досягаемости гейт обязан пропустить огонь"
print("UNIT_OK: fire-гейт держит недосягаемый транзит (hold), досягаемый — открывает огонь")

# 7c) HF неизвестен (нет hf_by / старый снапшот) -> fail-open, поведение прежнее
sent.clear(); RET["v"] = -0.0011
sf8 = make()                                      # shared без hf_by вовсе
sf8.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.9)
sf8.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) >= 1, "без HF гейт должен быть открыт (fail-open)"
print("UNIT_OK: HF неизвестен -> fail-open (без hf_by поведение прежнее)")

# 7d) ОТКАТ SPEC_FIRE_DENSE=0: прежний двухфазный каденс жив (burst=2 быстрых, дальше медленно)
sent.clear(); RET["v"] = -0.005
old_dense = sfl_mod.SPEC_FIRE_DENSE
old_slow = sfl_mod.SPEC_FIRE_SLOW_INTERVAL_SEC
old_fast = sfl_mod.SPEC_FIRE_INTERVAL_SEC
sfl_mod.SPEC_FIRE_DENSE = False
sfl_mod.SPEC_FIRE_INTERVAL_SEC = 0.05
sfl_mod.SPEC_FIRE_SLOW_INTERVAL_SEC = 30.0        # медленная фаза не успеет за тест
sf9 = make()
sf9.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.9)
sf9.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) == sfl_mod.SPEC_FIRE_BURST_PROBES == 2, sent
sfl_mod.SPEC_FIRE_DENSE = old_dense
sfl_mod.SPEC_FIRE_INTERVAL_SEC = old_fast
sfl_mod.SPEC_FIRE_SLOW_INTERVAL_SEC = old_slow
print("UNIT_OK: откат DENSE=0 возвращает прежний burst/slow-каденс")

# 8) send_spec_fire_probe: гейт inflight>0 молчит; газ-бакет стопит
ex._INFLIGHT = 1
assert ex.send_spec_fire_probe(None, cfg, "0x", 100000) is None
ex._INFLIGHT = 0
ex._SPECFIRE_GAS_USD = ex._SPECFIRE_MAX_GAS_USD + 1
assert ex.send_spec_fire_probe(None, cfg, "0x", 100000) is None
ex._SPECFIRE_GAS_USD = 0.0
print("UNIT_OK: probe-гейты (inflight, газ-бакет) атомарно закрывают отправку")

# 8b) WIN-cooldown: после приза окна не армятся (паттерн «пробы->приз» не палим)
sent.clear()
sf10 = make(receipt_fn=lambda h: (1, 0.02))
sfl_mod.SPEC_FIRE_COOLDOWN_TRIGGER = "any"                  # #46: дефолт combat — пауза за боевой
sfl_mod.SPEC_FIRE_COOLDOWN_SCOPE = "global"                 # экзаменуем ПРЕЖНЮЮ семантику
try:
    sf10._collect("0xw", "0xbtc", "0xFAT", 1_500_000.0)     # WIN -> кулдаун
    assert sf10._cooldown_until > 0
finally:
    sfl_mod.SPEC_FIRE_COOLDOWN_TRIGGER, sfl_mod.SPEC_FIRE_COOLDOWN_SCOPE = "combat", "feed"
sf10.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.3)
assert sf10.stats["windows_armed"] == 0, "в WIN-паузе окно не должно армиться"
sf10._cooldown_until = 0.0                               # пауза прошла
RET["v"] = -0.0011
sf10.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.2)
sf10.handle_event({"event": "disarm", "feed": "BTC"})
assert sf10.stats["windows_armed"] == 1, "после паузы армится как обычно"
ex._RECENT_SENDS.clear()
print("UNIT_OK: WIN-пауза рвёт паттерн, после неё армы штатные")

# 9) суточный бакет: на границе UTC-суток спек-газ сбрасывается (был lifetime — к вечернему
#    каскаду волатильного дня бакет пуст); specfire_gas_today кормит общий kill-switch
ex._SPECFIRE_GAS_USD = 5.0
assert ex.specfire_gas_today() == 5.0
ex._SPECFIRE_DAY -= 1                             # «наступили» новые сутки
assert ex.specfire_gas_today() == 0.0, "смена UTC-суток обязана сбросить бакет"
ex._SPECFIRE_GAS_USD = 0.0
print("UNIT_OK: суточный сброс бакета + specfire_gas_today для общего kill-switch")

# 10) ДОКТРИНА ПЛОТНОГО РЕЖИМА (31.07, гонка 14:07 проиграна деньгами — гонка плотностей):
#     дефолт DENSE=1, интервал 0.5±0.15 (4 пробы/блок), кап 150 = 75с покрытия >= p75 64с
assert sfl_mod.SPEC_FIRE_DENSE is True
assert sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC == 0.5
assert sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC == 0.15
assert sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW == 150, sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW
_cov = sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW * sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC
assert _cov >= 64.0, _cov                 # p75 «первая проба -> транзит» жирных окон = 64с
_per_block = 2.0 / sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC
assert _per_block >= 4.0, "плотность = рычаг: минимум 4 пробы на 2с-блок"
assert sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC >= 0.25, "пол send-слоя SPEC_FIRE_MIN_INTERVAL_SEC"
assert sfl_mod.SPEC_FIRE_REBUILD_SEC == 2.5, "свежесть пробы ~1 блок (2 ядра, «го» 31.07)"
print("UNIT_OK: плотный каденс %d проб × %.2f±%.2fс = %.0f проб/блок, покрытие %.0fс (p75 64с)" % (
    sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW, sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC,
    sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC, _per_block, _cov))

# 10b) джиттер плотного режима ломает периодичность, пол 0.25с не пробивается
import statistics as _st
_ivs = []
for _ in range(400):
    _j = sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC
    _ivs.append(max(0.25, sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC + __import__("random").uniform(-_j, _j)))
assert 0.35 <= min(_ivs) and max(_ivs) <= 0.65, (min(_ivs), max(_ivs))
assert _st.stdev(_ivs) > 0.05, "джиттер должен реально разбрасывать"
print("UNIT_OK: джиттер в [%.2f..%.2f]с, σ=%.2f — расписание нечитаемо" % (
    min(_ivs), max(_ivs), _st.stdev(_ivs)))

# 10c) цена худшего окна <= четверти суточного бакета (бакет держит 4+ полных окна,
#      WIN-cooldown 900с рвёт серию раньше, окно и так стопится на confirmed/disarm)
_probe_usd = 0.015
assert sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW * _probe_usd <= ex._SPECFIRE_MAX_GAS_USD * 0.25, "окно <= 25% бакета"
print("UNIT_OK: худшее окно ~$%.2f против суточного бакета $%.0f" % (
    sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW * _probe_usd, ex._SPECFIRE_MAX_GAS_USD))

# 11) КЭШ ПРОБЫ: на плотном каденсе _build (2×aggregate3 + роут) зовётся РАЗ в REBUILD_SEC,
#     не на каждую пробу; REBUILD_SEC=0 -> пересборка каждый раз
_builds = {"n": 0}
def build_cnt(mid, b, d):
    _builds["n"] += 1
    return {"calldata": "0xdeadbeef", "gas": 625_000}
sent.clear(); RET["v"] = -0.005
sf11 = make(build_fn=build_cnt)
sf11.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.9)
sf11.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) >= 2 and _builds["n"] == 1, (len(sent), _builds["n"])
old_rb = sfl_mod.SPEC_FIRE_REBUILD_SEC
sfl_mod.SPEC_FIRE_REBUILD_SEC = 0.0
sent.clear(); _builds["n"] = 0
sf11b = make(build_fn=build_cnt)
sf11b.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.9)
sf11b.handle_event({"event": "disarm", "feed": "BTC"})
assert _builds["n"] == len(sent) >= 2, (len(sent), _builds["n"])
sfl_mod.SPEC_FIRE_REBUILD_SEC = old_rb
print("UNIT_OK: кэш пробы — build 1 раз на TTL, плотность не душит RPC/ядро")

# 12) TIP-ГАРД: проба никогда не наследует каскадные ступени — кап 0.1 gwei поверх cfg
from chain.spec_fire_live import _probe_tip_wei
# #91: проба идёт на ПОЛУ рынка независимо от боевого tip. Прежние утверждения экзаменовали
# кап каскадной ступени и потому НЕ ловили дыру: кадансовая проба платила 0.05 = 50 полов и
# сортировалась в голову суб-блока, прочь от транзита (анализатор Ш10 на блоке 49789768).
import chain.execute as _ex_tip
assert _probe_tip_wei(0.05) == _ex_tip._PROBE_TIP_WEI, "кадансовая проба не на полу"
assert _probe_tip_wei(30.0) == _ex_tip._PROBE_TIP_WEI, "каскадная ступень протекла в пробу"
assert _probe_tip_wei(6.0) == _ex_tip._PROBE_TIP_WEI
assert _ex_tip._PROBE_TIP_WEI < int(0.05e9), "пол не ниже прежнего боевого tip — фикс пустой"
print("UNIT_OK: tip-гард — проба копеечная даже при поднятом боевом tip (FCFS)")

# 12b) send-слой уважает капнутый tip и в min_tip_wei (иначе поднял бы обратно до cfg)
import types as _t
_cap = {}
def _fake_send_tx(rpc, key, tx, wait=False, min_tip_wei=None, broadcast_raw_urls=None):
    _cap.update(tx=tx, min_tip=min_tip_wei)
    return {"hash": "0xh12"}
_olds = (ex.account_for, ex.send_tx, ex.probe_allowed_by_reserve, ex.cached_balance_wei)
ex.account_for = lambda w3, k: _t.SimpleNamespace(address="0xBOT")
ex.send_tx = _fake_send_tx
ex.probe_allowed_by_reserve = lambda bal, cost: True
ex.cached_balance_wei = lambda rpc, a: 10**19
ex._LOCAL_NONCE["0xBOT"] = 5
ex._SPECFIRE_LAST_TS = 0.0
_cfg30 = _t.SimpleNamespace(liquidator_address="0xLIQ", tip_gwei=30.0, wallet_key="k",
                            gas_limit_est=500_000, eth_price_usd=1730.0)
_rpcf = _t.SimpleNamespace(_web3=lambda: None)
_h = ex.send_spec_fire_probe(_rpcf, _cfg30, "0xdata", 100_000,
                             max_fee=10**9, tip=int(0.1e9))
assert _h == "0xh12" and _cap["min_tip"] == int(0.1e9), _cap
assert _cap["tx"]["maxPriorityFeePerGas"] == int(0.1e9) and _cap["tx"]["nonce"] == 5
ex.account_for, ex.send_tx, ex.probe_allowed_by_reserve, ex.cached_balance_wei = _olds
ex._LOCAL_NONCE.pop("0xBOT", None)
ex._SPECFIRE_LAST_TS = 0.0
print("UNIT_OK: send-слой шлёт пробу с капнутым tip (min_tip_wei не поднимает обратно)")

# 13) WIN стопит СВОЁ окно: приз взят — остаток капа не жжётся ревертами
sent.clear(); RET["v"] = -0.005
ex._RECENT_SENDS.clear()
sf13 = make(receipt_fn=lambda h: (1, 0.02))
sf13.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(1.0)
_n13 = len(sent)
assert sf13.stats["wins"] >= 1 and _n13 <= 2, (sf13.stats, _n13)
assert not sf13._windows, "после WIN окно обязано быть снято"
time.sleep(0.7)
assert len(sent) == _n13, "после WIN проб быть не должно"
ex._RECENT_SENDS.clear()
print("UNIT_OK: WIN снимает окно (кап не жжётся) + кулдаун армов")

# 14) TOP-K ротация (K=2): пробы чередуются по двум самым жирным досягаемым целям
old_k = sfl_mod.SPEC_FIRE_TOPK
sfl_mod.SPEC_FIRE_TOPK = 2
shared2 = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT", "0xFAT2", "0xdust"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0, ("0xbtc", "0xFAT2"): 1_200_000.0,
             ("0xbtc", "0xdust"): 90_000.0},
    by_id={"0xbtc": mk("cbBTC", "USDC")},
)
sent.clear(); RET["v"] = -0.005
sf14 = SpecFireLive(rpc=None, cfg=cfg, shared=shared2, ret_fn=lambda l: RET["v"],
                    dev_by={"BTC": 0.0010}, build_fn=lambda mid, b, d: {"calldata": b, "gas": 1},
                    send_fn=send_fn, receipt_fn=receipt_fn, log_fn=LOGS.append)
sf14.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(1.3)
sf14.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) >= 2 and sent[0][0] == "0xFAT" and sent[1][0] == "0xFAT2", sent
sfl_mod.SPEC_FIRE_TOPK = old_k
print("UNIT_OK: top-K=2 чередует пробы по жирнейшим целям (дефолт K=1 = прежний top-1)")

# 15) _receipt: ручной полл раз в RECEIPT_POLL_SEC (НЕ wait_for_transaction_receipt с его
#     дефолтным 0.1с — 6-8 параллельных сборщиков дали бы 60+ req/с)
class _FEth:                                       # у фейка НЕТ wait_for_transaction_receipt
    def __init__(self):
        self.calls = 0
    def get_transaction_receipt(self, h):
        self.calls += 1
        if self.calls < 3:
            raise LookupError("not found")
        return {"status": 1, "gasUsed": 115_000, "effectiveGasPrice": 50_000_000,
                "blockNumber": 49358741, "transactionIndex": 2929}
_slept = []
sf15 = SpecFireLive(rpc=types.SimpleNamespace(_web3=lambda _e=_FEth(): types.SimpleNamespace(eth=_e)),
                    cfg=cfg, shared=shared, ret_fn=lambda l: 0.0, dev_by={"BTC": 0.001},
                    build_fn=build_fn, send_fn=send_fn, log_fn=LOGS.append,
                    sleep=lambda s: _slept.append(s))
_r15 = sf15._receipt("0xh")
assert _r15[0] == 1 and _slept == [sfl_mod.SPEC_FIRE_RECEIPT_POLL_SEC] * 2, (_r15, _slept)
assert 0.005 < _r15[1] < 0.02 and _r15[2] == 49358741 and _r15[3] == 2929, _r15
print("UNIT_OK: receipt — ручной полл 1.0с + blk/idx (метрика гонки плотностей из журнала)")

# 15b) _collect пишет blk/idx в лог реверта; старые 2-кортежи receipt_fn не ломаются
LOGS.clear()
sf15b = make(receipt_fn=lambda h: (0, 0.011, 49358741, 2929))
sf15b._collect("0xh", "0xbtc", "0xFAT", 1_500_000.0)
assert any("blk=49358741 idx=2929" in m for m in LOGS), LOGS
sf15c = make(receipt_fn=lambda h: (0, 0.011))          # прежний 2-кортеж — совместимость
sf15c._collect("0xh", "0xbtc", "0xFAT", 1_500_000.0)
print("UNIT_OK: idx пробы в журнале, 2-кортежные receipt_fn совместимы")

# 16) LOSS-стоп: жертву разобрал конкурент (bs==0 -> _build='gone') — окно закрывается,
#     кап не догорает ревертами по мёртвой цели
_calls16 = {"n": 0}
def build_gone(mid, b, d):
    _calls16["n"] += 1
    return {"calldata": "0xdead", "gas": 1} if _calls16["n"] == 1 else "gone"
sent.clear(); LOGS.clear(); RET["v"] = -0.005
old_rb = sfl_mod.SPEC_FIRE_REBUILD_SEC
sfl_mod.SPEC_FIRE_REBUILD_SEC = 0.0                    # пересборка каждый раз — 'gone' со 2-й
sf16 = make(build_fn=build_gone)
sf16.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(1.3)
sf16.handle_event({"event": "disarm", "feed": "BTC"})
sfl_mod.SPEC_FIRE_REBUILD_SEC = old_rb
assert len(sent) == 1, sent
assert any("loss-stop" in m for m in LOGS), LOGS
print("UNIT_OK: LOSS-стоп — разобранная цель снята, окно не жжёт кап")

# 16b) LOSS-стоп при TOPK=2: мёртвая цель снята, огонь продолжается по живой
sfl_mod.SPEC_FIRE_TOPK = 2
sfl_mod.SPEC_FIRE_REBUILD_SEC = 0.0
def build_fat_gone(mid, b, d):
    return "gone" if b == "0xFAT" else {"calldata": b, "gas": 1}
sent.clear(); LOGS.clear(); RET["v"] = -0.005
sf16b = SpecFireLive(rpc=None, cfg=cfg, shared=shared2, ret_fn=lambda l: RET["v"],
                     dev_by={"BTC": 0.0010}, build_fn=build_fat_gone, send_fn=send_fn,
                     receipt_fn=receipt_fn, log_fn=LOGS.append)
sf16b.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(1.2)
sf16b.handle_event({"event": "disarm", "feed": "BTC"})
sfl_mod.SPEC_FIRE_TOPK = old_k
sfl_mod.SPEC_FIRE_REBUILD_SEC = old_rb
assert len(sent) >= 1 and all(c == "0xFAT2" for c, _ in sent), sent
assert any("loss-stop" in m for m in LOGS), LOGS
print("UNIT_OK: LOSS-стоп при K=2 — мёртвая снята, живая под огнём")

# 17) КАСКАДНЫЙ ПРОТОКОЛ БАКЕТА: не подписан (env 0) -> бустер мёртв; подписан ->
#     кап поднимается на TTL и сам возвращается
assert ex._SPECFIRE_CASCADE_GAS_USD == 0.0, "дефолт: протокол НЕ подписан"
assert ex.specfire_cascade_boost(now=100.0) is False
assert ex._specfire_cap(now=101.0) == ex._SPECFIRE_MAX_GAS_USD
ex._SPECFIRE_CASCADE_GAS_USD = 100.0                   # «подпись владельца»
assert ex.specfire_cascade_boost(ttl_sec=3600.0, now=200.0) is True
assert ex._specfire_cap(now=200.0 + 3599.0) == 100.0, "каскадный кап при живом TTL"
assert ex._specfire_cap(now=200.0 + 3601.0) == ex._SPECFIRE_MAX_GAS_USD, "TTL вышел -> штатный"
ex.specfire_cascade_boost(ttl_sec=3600.0, now=200.0 + 3000.0)   # продление армом
assert ex._specfire_cap(now=200.0 + 3601.0) == 100.0, "новый арм продлевает TTL"
ex._SPECFIRE_CASCADE_GAS_USD = 0.0
ex._SPECFIRE_CASCADE_UNTIL = 0.0
print("UNIT_OK: каскад-бустер мёртв без подписи, с подписью TTL-буст и авто-возврат")

# 18) каскад-детект (гибрид, калибровка 31.07): 4-й арм за час дёргает бустер ТОЛЬКО при
#     свежем факт-флаге (детали гибрида/fail-closed — test_cascade_hybrid.py), лог троттлится
import os as _os, tempfile as _tf
_fd, _flag18 = _tf.mkstemp()
_os.close(_fd)
_old_flag18 = sfl_mod.SPEC_FIRE_FACTS_FLAG
sfl_mod.SPEC_FIRE_FACTS_FLAG = _flag18
ex._SPECFIRE_CASCADE_GAS_USD = 100.0
ex._SPECFIRE_CASCADE_UNTIL = 0.0
LOGS.clear(); RET["v"] = -0.0005                       # ниже порога: окна армятся, огня нет
sf18 = make()
for _ in range(sfl_mod.SPEC_FIRE_CASCADE_WINDOWS):
    sf18.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
sf18.handle_event({"event": "disarm", "feed": "BTC"})
assert ex._SPECFIRE_CASCADE_UNTIL > 0.0, "4 арма/час + факт-флаг обязаны дёрнуть бустер"
assert sum(1 for m in LOGS if "каскад-детект" in m) == 1, LOGS
ex._SPECFIRE_CASCADE_GAS_USD = 0.0
ex._SPECFIRE_CASCADE_UNTIL = 0.0
_os.remove(_flag18)
sfl_mod.SPEC_FIRE_FACTS_FLAG = _old_flag18
print("UNIT_OK: каскад-детект — %d окон/час при свежем факт-флаге дёргают бустер" %
      sfl_mod.SPEC_FIRE_CASCADE_WINDOWS)

print("=== ВСЕ SPEC-FIRE-LIVE ЮНИТЫ ПРОШЛИ (плотный режим + LOSS-стоп + каскад-протокол) ===")
