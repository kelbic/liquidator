"""ПРИЁМКА (б) — НА ОКНЕ, А НЕ НА ПРОЦЕССЕ (#110, ревью р-3, условие 3).

ДЕФЕКТ, КОТОРЫЙ ЗАКРЫВАЕТСЯ. Критерий приёмки боем «probe_no_agg==0» стоял на КУМУЛЯТИВНОМ
счётчике `SpecFireLive.stats`: словарь заводится один раз в `__init__` и умеет только расти.
Один ЗАКОННЫЙ слепой суд (рынок, чей watch-ключ не адрес — напр. LBTC_FUNDAMENTAL из
WATCH_PATTERN_OVERRIDES, `agg_set` отбрасывает такие ключи) защёлкивал приёмку ПРОВАЛЕННОЙ на
всю жизнь процесса: репро ревьюера — 1 слепой суд + 200 ЗРЯЧИХ замеров дают probe_judged=201,
in_band=200, tip_match=200 и probe_no_agg=1 навсегда. Плюс строка «SPECFIRE ПОЗИЦИИ» вслепую
и знаючи выглядела БАЙТ В БАЙТ одинаково (в слепом случае печатается адрес ЭВРИСТИЧЕСКИ
выбранного, скорее всего ЧУЖОГО транзита) — жирное окно было нечитаемо.

ЧТО ЗДЕСЬ НАСТОЯЩЕЕ: `ChainIndex` на записанном ответе узла (блок 49890266, два транзита 406
и 1083, 33 наши пробы), `SpecFireLive._note_probe_position`, `_agg_of_market`, `_probe_snap`,
`_probe_delta`, `feeds.resolve_feeds`, `our_position_report` и — во второй части — НАСТОЯЩИЙ
`_window_loop` в НАСТОЯЩЕМ треде: снимок обязан браться на АРМЕ окна, а дельта попадать в
закрывающую строку. Подменены только сокет узла и I/O-шов резолва ног (`_resolve_legs`) —
ровно те же швы, что у штатных тестов пакета.

ОДИН ОБЪЕКТ НА ДВА ОКНА — это и есть суть: штатный тест `note_position()` строил НОВЫЙ
SpecFireLive на каждый случай, то есть судил всегда «окно 1», и латч был для него невидим.
"""
import gzip
import json
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402
import chain.feeds as feeds                                   # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
BN = 49890266
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
AGG_PRIZE = "0x9491aedfe0da70eb110582b66b2edace28d82a5d"      # владелец транзита idx=1083
AGG_LEG2 = "0x" + "c3" * 20                                    # вторая нога того же рынка
MID_SEEN = "0x" + "11" * 32                                    # рынок с резолвнутыми ногами
MID_BLIND = "0x" + "22" * 32                                   # рынок без ног (watch-паттерн)

BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS_FX = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RECEIPTS = json.loads((FIX / "receipts_49890266.json").read_text())
H_FIRE = next(rc["transactionHash"] for rc in RECEIPTS if rc["to"].lower() == BOARD)[2:]


def _http(url, payload, timeout):
    m = payload["method"]
    if m == "eth_getLogs":
        return {"result": LOGS_FX}
    if m == "eth_getBlockByNumber":
        return BLOCK
    if m == "eth_getBlockReceipts":
        return {"result": RECEIPTS}
    if m == "eth_blockNumber":
        return {"result": hex(BN)}
    raise AssertionError(m)


# ---- настоящий индекс, прогретый БОЕВЫМ путём (наша проба в полёте) ----
idx = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=_http,
                    now=lambda: 0.0, log_fn=lambda m: None)
idx.note_hash(H_FIRE, head=BN - 1)
idx.scan_block(BN)
FACTS = idx.block_facts(BN)
assert FACTS and len(FACTS["ours"]) == 33, "фикстура не та: %s" % (FACTS or {}).get("ours")

# ---- карта владения: настоящий resolve_feeds, подменён только I/O-шов ног ----
_sv_legs = feeds._resolve_legs
_mkt = lambda mid: types.SimpleNamespace(market_id=mid)                       # noqa: E731
try:
    feeds._resolve_legs = lambda rpc, mid, fb=None: (
        (AGG_PRIZE, [AGG_PRIZE, AGG_LEG2]) if mid == MID_SEEN else (None, []))
    feeds.resolve_feeds(None, [_mkt(MID_SEEN), _mkt(MID_BLIND)])
finally:
    feeds._resolve_legs = _sv_legs
assert feeds.legs_map().get(MID_SEEN) and MID_BLIND not in feeds.legs_map(), feeds.legs_map()


class _Cfg:
    liquidator_address = BOARD
    eth_price_usd = 3000.0


LOG = []


def make_sf():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf.cfg = _Cfg()
    sf.rpc = None
    sf.stats = {}
    # УЧЁТ ПОЗИЦИЙ ПО КЛЮЧУ ПЛАНА (р-6, блокер C): в бою эти поля заводит __init__, здесь
    # объект собран через __new__ — заводим ровно те же, иначе стенд проверял бы объект,
    # которого в бою не бывает.
    sf._probe_by_key = {}
    sf._win_key = {}
    sf._log = LOG.append
    sf._now = lambda: 0.0
    return sf


def probe_receipt(want_blind):
    """Квитанция НАШЕЙ пробы в форме, в какой её отдаёт коллектор (индекс или сеть)."""
    h, f = sorted(FACTS["ours"].items(), key=lambda kv: kv[1]["index"])[0]
    return h, ci.receipt_like(f) or {"blockNumber": hex(BN),
                                     "transactionIndex": hex(f["index"])}


_saved_idx = ci._INDEX
ci._INDEX = idx
try:
    sf = make_sf()                       # ОДИН объект на ОБА окна — как в бою
    h, r = probe_receipt(True)

    # ------------------------------------------------ ОКНО 1: суд ВСЛЕПУЮ (законный случай)
    w1 = sf._probe_snap()
    LOG.clear()
    sf._note_probe_position(h, r, meta=("0xleg", ("LBTC", MID_BLIND, "0xb", 1.0)))
    d1 = sf._probe_delta(w1)
    line1 = [m for m in LOG if "ПОЗИЦИИ" in m]
    assert d1["probe_no_agg"] == 1 and d1["probe_judged"] == 1 and d1["probe_judged_blind"] == 1, d1
    assert d1.get("probe_in_band", 0) == 0 and d1.get("probe_tip_match", 0) == 0, \
        "вслепую денежные счётчики расти НЕ имеют права: %s" % d1
    assert line1 and "ВСЛЕПУЮ (эвристика)" in line1[0], line1
    print("UNIT_OK: окно 1 — законный слепой суд: дельта %s, строка называет режим вслух" % d1)

    # ------------------------------------------------ ОКНО 2: ЖИРНОЕ, владелец известен
    w2 = sf._probe_snap()
    LOG.clear()
    sf._note_probe_position(h, r, meta=("0xleg", ("CBBTC", MID_SEEN, "0xb", 1.0)))
    d2 = sf._probe_delta(w2)
    line2 = [m for m in LOG if "ПОЗИЦИИ" in m]
    assert d2["probe_no_agg"] == 0 and d2["probe_judged_blind"] == 0 and d2["probe_judged"] == 1, \
        ("окно 2 судилось ЗНАЯ владельца — приёмка (б) обязана читаться нулём В ЭТОМ окне: %s"
         % d2)
    assert sf.stats["probe_no_agg"] == 1, \
        ("кумулятив обязан ОСТАТЬСЯ единицей — иначе тест ничего не доказал: %s" % sf.stats)
    assert line2 and "владелец известен (2 ног)" in line2[0], line2
    assert line1[0] != line2[0], "слепая и зрячая строки обязаны РАЗЛИЧАТЬСЯ"
    # ЧЕСТНЫЙ ЗНАМЕНАТЕЛЬ: на этом настоящем блоке ни одна из 33 наших проб в полосу за
    # призовым транзитом не попала (ближайшая — на 61 позицию позже). Это и есть замер
    # проигрыша, ради которого метрика №1 заводилась; фабриковать «попадание» тест не станет.
    assert d2.get("probe_in_band", 0) == 0, d2
    print("UNIT_OK: окно 2 — суд ЗНАЯ владельца: дельта окна probe_no_agg=0 при кумулятиве "
          "процесса probe_no_agg=%d (прежний критерий был бы «провален» навсегда)"
          % sf.stats["probe_no_agg"])
    print("UNIT_OK: строка ПОЗИЦИИ называет режим суда — «%s» против «%s»"
          % (line1[0].split("[суд: ")[1].split("]")[0],
             line2[0].split("[суд: ")[1].split("]")[0]))
finally:
    ci._INDEX = _saved_idx

# ------------------------------------------------ ЗАКРЫВАЮЩАЯ СТРОКА ОКНА — НАСТОЯЩАЯ ПЕТЛЯ
# Снимок обязан браться в `_window_loop` НА АРМЕ, а дельта — попадать в close-строку. Без
# этого проверялась бы только формула, а шов «где именно снимается снимок» остался бы
# подменённым выше дефекта.
_shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
_cfg2 = types.SimpleNamespace(liquidator_address=BOARD, gas_limit_est=500_000, tip_gwei=0.05,
                              sequencer_rpc="off", eth_price_usd=3000.0,
                              fast_lane_max_repaid_usd=500_000.0)
_sv_board = sfl.SPEC_FIRE_V2_BOARD
LOG2 = []
_saved_idx = ci._INDEX
ci._INDEX = idx
try:
    sfl.SPEC_FIRE_V2_BOARD = False            # доска — не предмет этого теста
    live = sfl.SpecFireLive(rpc=None, cfg=_cfg2, shared=_shared, ret_fn=lambda lbl: 0.0,
                            dev_by={"BTC": 0.001}, build_fn=lambda m, b, d: None,
                            send_fn=lambda c, g: None, receipt_fn=lambda h: (0, 0.0),
                            log_fn=LOG2.append)
    # ЛАТЧ ДО ОКНА: законный слепой суд в РАННЕМ окне ТОГО ЖЕ ФИДА. Фид здесь именно «BTC»
    # (после р-5 счётчики пофидовые): возьми мы чужой фид — слепой суд не попал бы в дельту и
    # без снимка, и мутация «снимок на арме не берётся» прошла бы зелёной. Стенд обязан
    # отличать «дельта» от «итог», а не только уметь их складывать.
    live._note_probe_position(h, r, meta=("0xleg", ("BTC", MID_BLIND, "0xb", 1.0)))
    assert live.stats["probe_no_agg"] == 1, live.stats
    # ...И СУД ТОГО ЖЕ КЛЮЧА ДО ОКНА. Р-7: приёмка окна судится ПО КЛЮЧУ ПЛАНА, поэтому чужой
    # ключ отсекается и без снимка — мутация «снимок не берётся» прошла бы зелёной. Ранний суд
    # ПОД ТЕМ ЖЕ КЛЮЧОМ, что и план окна, оставляет снимок ЕДИНСТВЕННЫМ, что его отсекает.
    live._note_probe_position(h, r, meta=("0xleg", ("BTC", MID_SEEN, "0xb", 1.0)))
    stop = threading.Event()
    # ПЛАН ЭТОГО ОКНА (блокер C р-6 / B2 р-7): без ключа окно не смеет печатать число вовсе.
    # В бою ключ кладёт _prearm_board; здесь доска выключена нарочно (V2_BOARD=False), поэтому
    # ставим его руками — иначе стенд мерил бы объект, которого в бою не бывает.
    live._win_key["BTC"] = live.plan_key(MID_SEEN, "0xb")
    t = threading.Thread(target=live._window_loop, args=("BTC", "down", stop), daemon=True)
    t.start()
    time.sleep(0.15)                          # окно живёт; снимок уже взят на арме
    live._note_probe_position(h, r, meta=("0xleg", ("BTC", MID_SEEN, "0xb", 1.0)))
    time.sleep(0.05)
    stop.set()
    t.join(timeout=5)
    close = [m for m in LOG2 if "window close" in m]
    assert close, LOG2[-3:]
    assert "окно_позиции=" in close[0], \
        ("закрывающая строка окна обязана нести ДЕЛЬТУ за это окно, иначе приёмка (б) читается "
         "по пожизненному итогу: %s" % close[0])
    _w = close[0].split("окно_позиции=")[1].split(" stats=")[0]
    assert "'probe_no_agg': 0" in _w and "'probe_judged': 1" in _w, \
        ("снимок обязан браться НА АРМЕ окна: суд ДО окна не имеет права попадать в дельту "
         "этого окна ДАЖЕ ПОД ТЕМ ЖЕ КЛЮЧОМ — %s (кумулятив %s)" % (_w, live.stats))
    assert live.stats["probe_no_agg"] == 1 and live.stats["probe_judged"] == 3, live.stats
    print("UNIT_OK: НАСТОЯЩИЙ _window_loop берёт снимок на арме и печатает дельту в закрывающей "
          "строке: окно_позиции=%s при пожизненном probe_no_agg=%d"
          % (_w, live.stats["probe_no_agg"]))

    # ---------------------------------------------- B2 р-7: ОКНО БЕЗ КЛЮЧА ПЛАНА НЕ ПЕЧАТАЕТ ЧИСЕЛ
    # Прежде такое окно печатало дельту КУМУЛЯТИВА ПРОЦЕССА как свою приёмку: чужие пробы
    # выдавались за измерение окна, которое не публиковало ничего. Незнание закрывает гард.
    LOG2.clear()
    live.dev_by["ETH"] = 0.001
    stop2 = threading.Event()
    live._win_key.pop("ETH", None)
    t2 = threading.Thread(target=live._window_loop, args=("ETH", "down", stop2), daemon=True)
    t2.start()
    time.sleep(0.10)
    live._note_probe_position(h, r, meta=("0xleg", ("ETH", MID_SEEN, "0xb", 1.0)))
    time.sleep(0.05)
    stop2.set()
    t2.join(timeout=5)
    _c2 = [m for m in LOG2 if "window close" in m and "feed=ETH" in m]
    assert _c2, LOG2[-3:]
    _w2 = _c2[0].split("окно_позиции=")[1].split(" stats=")[0]
    assert "не измерено" in _w2 and not any(ch.isdigit() for ch in _w2), \
        ("окно БЕЗ ключа плана обязано сказать «не измерено» и не печатать НИ ОДНОЙ цифры "
         "(ноль — тоже число): %s" % _w2)
    print("UNIT_OK: окно без ключа плана печатает «%s» — кумулятив процесса больше не выдаётся "
          "за приёмку окна" % _w2)
finally:
    sfl.SPEC_FIRE_V2_BOARD = _sv_board
    ci._INDEX = _saved_idx

print("ПРОШЛИ: приёмка (б) судится НА ОКНЕ, режим суда назван вслух")
