"""ХВОСТ #110-х1 ПОСЛЕ РЕЗА: победа через NO-RECEIPT УЧИТЫВАЕТСЯ, но ОГНЁМ НЕ УПРАВЛЯЕТ.

ЧТО БЫЛО ДО ПАКЕТА. `_engine_timeout` спрашивает индекс первым и на ответе 'mined' книжит газ,
спасает склад ноги и считает позицию — но `fact['won']` не читал ВООБЩЕ. Победа, о которой узел
квитанций промолчал (а он молчит регулярно: 889 «collect batch fail» за сутки и ЛОЖНЫЕ
NO-RECEIPT у замайненных проб j1/j2), не попадала в `wins` ВОВСЕ: числитель win-rate брался бы
только из ветки квитанции при знаменателе по обоим источникам.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ ТЕПЕРЬ (рез #110, решение архитектора). Слой ПОБЕДЫ — гашение окна,
WIN-пауза, `disarm` — из пакета УДАЛЁН и заморожен до решений владельца, потому что три раунда
подряд давал то ложное действие, то бездействие. На пути NO-RECEIPT его в бою НИКОГДА и не было:
он появился в р-6 вместе со слоем. Осталось РОВНО ИЗМЕРЕНИЕ ДЕНЕГ:

  * `won is True`  -> `wins` +1 И `noreceipt_win` +1, строка победы СЛЫШНА (WARNING) и НАЗЫВАЕТ
    цену заморозки: окно не гасится, пауза не ставится, огонь снимется по TTL окна;
  * `won is False` -> не победа (логи прочитаны, приза нет);
  * `won is None`  -> НЕЗНАНИЕ: победой не считается и НАЗЫВАЕТСЯ в строке исхода.

И ГЛАВНОЕ, ЧТО ЭТОТ ФАЙЛ ДЕРЖИТ ПОСЛЕ РЕЗА: путь NO-RECEIPT НЕ ТРОГАЕТ ОГОНЬ — ни `_windows`,
ни кулдаун, ни `disarm`. Это не «побочно так вышло», это ПРЕДМЕТ решения: пока слой заморожен,
любое действие отсюда есть возврат срезанного механизма чёрным ходом.

НАСТОЯЩИЕ: `build_facts` и `parse_logs` (PURE-ядро индекса) на собранном блоке, `_ingest`,
`state`, `_engine_timeout`, `_set_cooldown`, настоящий `FireEngine.disarm/armed`.
Подменены сокет и часы.
"""
import logging
import pathlib
import sys
import threading
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402
import chain.fire_engine as fe                                # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

BN = 49890266
OURS = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"           # наш контракт (v5 доска)
MORPHO = ci.MORPHO_BLUE
MID = "0x" + "11" * 32
BORROWER = "0x" + "aa" * 20
LEG = "0x" + "cc" * 20
TXH = "dd" * 32
LOGGER = logging.getLogger("liq.spec_fire_live")   # тот самый логгер боевого модуля


def _block(tx_to=OURS):
    return {"number": hex(BN), "baseFeePerGas": hex(10),
            "transactions": [{"hash": "0x" + TXH, "to": tx_to, "from": LEG,
                              "transactionIndex": hex(11),
                              "maxPriorityFeePerGas": hex(1_000_000), "type": "0x2"}]}


def _logs(won=True):
    """Лог Liquidate у Morpho с НАШИМ хэшем — это и есть захват приза на цепи."""
    if not won:
        return []
    return [{"address": MORPHO, "transactionHash": "0x" + TXH, "logIndex": hex(1),
             "topics": [ci.MORPHO_LIQUIDATE_TOPIC, MID,
                        "0x" + "00" * 12 + OURS[2:], "0x" + "00" * 12 + BORROWER[2:]],
             "data": "0x" + "00" * 32 * 6}]


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, rec):
        self.rows.append((rec.levelno, rec.getMessage()))


def drill(won=True, logs_read=True, kill_verdict=False):
    """Проба сгорела по таймауту коллектора, а ИНДЕКС видел её в блоке.

    won — взяла ли проба приз на цепи; logs_read=False — логи блока не читаны (won=None);
    kill_verdict — МУТАЦИЯ «ветка mined слепа к вердикту индекса» (редакция до р-6).
    """
    facts = ci.build_facts(_block(), _logs(won) if logs_read else None,
                           watch_to=[OURS])
    assert facts and facts["ours"], "дрель пуста: наша tx обязана быть в фактах блока"
    _f = list(facts["ours"].values())[0]
    assert _f.get("won") is (True if (logs_read and won) else (False if logs_read else None)), \
        ("вердикт `won` в факте обязан быть трёхзначным (True/False/None): %s" % _f.get("won"))

    idx = ci.ChainIndex("https://node.test/K", watch_to=[OURS],
                        http=lambda u, p, t: {"result": None}, now=lambda: 1000.0,
                        log_fn=lambda m: None)
    idx._ingest(BN, facts, full=True)

    log_rows = []
    cfg = types.SimpleNamespace(liquidator_address=OURS, eth_price_usd=3000.0,
                                gas_limit_est=500_000, tip_gwei=0.05, sequencer_rpc="off",
                                fast_lane_max_repaid_usd=500_000.0)
    shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
    live = sfl.SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                            dev_by={"BTC": 0.001}, build_fn=lambda m, b, d: None,
                            send_fn=lambda c, g: None, receipt_fn=lambda h: None,
                            log_fn=log_rows.append)
    eng = fe.FireEngine(legs_fn=lambda: [], send_fn=lambda a: None, threads=0, rate=1.0)
    eng.keepalive(target=("BTC", MID, BORROWER, 90_000.0), ttl=600.0)
    live._fire_engine = eng
    stop = threading.Event()
    live._windows["BTC"] = stop
    # ПЛАН ЭТОГО ОКНА — ИЗМЕРИТЕЛЬНЫЙ КЛЮЧ (приёмка (б) окна), и БОЛЬШЕ НИЧЕЙ. До реза этот же
    # словарь решал, чьё окно гасить при победе; после реза он не участвует в решениях вовсе,
    # и ставится здесь ровно затем, чтобы дрель мерила боевую форму объекта.
    live._plan_key_by_feed["BTC"] = live.plan_key(MID, BORROWER)
    live._idx_for_b1 = idx          # тот же индекс нужен клетке 5 (повторный прогон вердикта)
    # СПИОН НА КУЛДАУНЕ: путь NO-RECEIPT не имеет права ставить WIN-паузу — слой заморожен.
    cd_calls = []
    _real_cd = live._set_cooldown
    live._set_cooldown = lambda feed, src, proven=False: (
        cd_calls.append((feed, src, proven)) or _real_cd(feed, src, proven=proven))
    live._cd_calls = cd_calls
    if kill_verdict:
        # МУТАЦИЯ: ветка mined снова слепа к вердикту индекса (редакция до р-6). Бьём по САМОМУ
        # ЧТЕНИЮ вердикта, а не по действию: после реза действия здесь нет, и мутация «убрать
        # _probe_win» краснить стенд больше не может — она стала штатным состоянием.
        _st_real = idx.state
        idx.state = lambda h: ((lambda s, f: (s, dict(f or {}, won=None)))(*_st_real(h)))

    cap = _Cap()
    cap.setLevel(logging.DEBUG)
    LOGGER.addHandler(cap)
    _sv_idx, _lvl = ci._INDEX, LOGGER.level
    ci._INDEX = idx
    LOGGER.setLevel(logging.INFO)
    try:
        live._engine_timeout((LEG, ("BTC", MID, BORROWER, 90_000.0)), TXH)
    finally:
        ci._INDEX = _sv_idx
        LOGGER.setLevel(_lvl)
        LOGGER.removeHandler(cap)
    return live, eng, stop, cap, log_rows


# ---------------------------------------------------------------- 0. премиса дрели
_l, _e, _s, _c, _rows = drill(won=False)
assert _l.stats.get("noreceipt_false"), \
    ("дрель обязана идти по ветке 'mined' (индекс ОПРОВЕРГ NO-RECEIPT): %s" % _l.stats)
assert not _l.stats.get("wins") and not _l.stats.get("noreceipt_win"), _l.stats
assert _e.armed() is True and not _s.is_set(), \
    "проба села, но приза не взяла — гасить нечего ни при каком слое"
print("UNIT_OK: ветка NO-RECEIPT+mined без победы (won=False — это ФАКТ, логи прочитаны) "
      "победу не считает и огня не трогает")

# ---------------------------------------------------------------- 1. ПОБЕДА УЧТЕНА (ДЕНЬГИ)
live, eng, stop, cap, rows = drill(won=True)
assert live.stats.get("wins") == 1 and live.stats.get("noreceipt_win") == 1, \
    ("победа, увиденная ИНДЕКСОМ, обязана считаться победой — иначе числитель win-rate берётся "
     "только из ветки квитанции при знаменателе по обоим источникам: %s" % live.stats)
_win_lines = [m for lv, m in cap.rows if "SPECFIRE WIN!!!" in m and "NO-RECEIPT" in m]
assert _win_lines and [lv for lv, m in cap.rows if "SPECFIRE WIN!!!" in m][0] >= logging.WARNING, \
    ("первая победа обязана быть СЛЫШНА и назвать источник вердикта: %s" % cap.rows[-3:])
print("UNIT_OK: победа через NO-RECEIPT ВИДНА В УЧЁТЕ: wins=1, noreceipt_win=1, строка «%s»"
      % _win_lines[0][:80])

# ---------------------------------------------------------------- 2. РЕЗ: ОГОНЬ НЕ ТРОГАЕМ
# Слой победы заморожен и удалён (#110). Путь NO-RECEIPT в бою НИКОГДА не гасил окно и не
# ставил паузу — это добавил р-6. Возврат любого из трёх действий здесь = возврат срезанного
# механизма чёрным ходом, поэтому проверяется КАЖДОЕ.
assert eng.armed() is True, \
    ("рез #110: путь NO-RECEIPT не имеет права разоружать движок — слой победы заморожен, "
     "и его возврат делается ПАКЕТОМ после решений владельца, а не отсюда")
assert not stop.is_set(), \
    "рез #110: окно с этого пути не гасится (в бою оно и не гасилось НИ РАЗУ)"
assert live._cd_calls == [], \
    ("рез #110: WIN-пауза с этого пути не ставится: %s" % live._cd_calls)
assert any("слой победы заморожен" in m for lv, m in cap.rows), \
    ("ЦЕНА ЗАМОРОЗКИ ОБЯЗАНА БЫТЬ В ЖУРНАЛЕ, а не выводиться из кода: движок может жечь газ "
     "по уже взятой цели до конца TTL окна, и владелец решает по этой строке: %s" % cap.rows[-2:])
print("UNIT_OK: рез — путь NO-RECEIPT не гасит окно, не ставит паузу и не разоружает движок; "
      "цена заморозки НАЗВАНА в той же строке")

# ---------------------------------------------------------------- 3. НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД
live_u, eng_u, stop_u, cap_u, rows_u = drill(won=True, logs_read=False)
assert not live_u.stats.get("wins") and not live_u.stats.get("noreceipt_win"), \
    ("won=None — это «логи не читаны», а не победа: засчитать её значило бы выдать незнание за "
     "денежный факт: %s" % live_u.stats)
# СЧЁТЧИК «ВЕРДИКТ НЕИЗВЕСТЕН» СНЯТ В Р-7 как структурно нулевой: здесь он «достижим» только
# потому, что дрель зовёт build_facts(logs=None) РУКАМИ, а боевой scan_block читает логи ПЕРВЫМ
# вызовом и на отказе бросает IndexUnavailable. Ноль в таком счётчике одинаково значил «вердикт
# всегда есть» и «ветка мертва». Требование осталось прежним — незнание обязано быть НАЗВАНО, —
# но названо оно теперь там, где его прочтёт человек: в строке самого исхода.
assert any("вердикт победы=None" in m for m in rows_u), \
    ("недоступность вердикта обязана быть НАЗВАНА в строке исхода: %s" % rows_u[-2:])
print("UNIT_OK: won=None (логи блока не читаны) победой НЕ считается и НАЗВАНО в строке исхода "
      "(«вердикт победы=None») — незнание закрывает гард, а не открывает его")

# ---------------------------------------------------------------- 4. МУТАЦИЯ: вернуть слепоту
# Мутация бьёт по САМОМУ ЧТЕНИЮ вердикта — единственному, что от хвоста #110-х1 осталось.
# Если стенд её переживёт, файл не держит ничего: «победа NO-RECEIPT невидима» вернётся молча.
live_m, eng_m, stop_m, cap_m, _rm = drill(won=True, kill_verdict=True)
assert not live_m.stats.get("wins") and not live_m.stats.get("noreceipt_win"), \
    ("мутация «ветка mined слепа к вердикту индекса» (редакция до р-6) ОБЯЗАНА краснить "
     "стенд — иначе учёт побед NO-RECEIPT можно потерять незаметно: %s" % live_m.stats)
print("UNIT_OK: мутация (ветка mined слепа к fact['won'], как было) краснит стенд: победа "
      "исчезает из учёта")

# ---------------------------------------------------------------- 5. ЯРЛЫК НИЧЕГО НЕ РЕШАЕТ
# До реза здесь стояла клетка B1: победа под чужим ЯРЛЫКОМ обязана была гасить окно ПЛАНА.
# Механизм срезан, и проверять «правильную атрибуцию» больше не у чего. Осталось требование
# слабее и честнее: чужой ярлык не имеет права ТРОГАТЬ чужое окно — потому что этот путь не
# трогает НИКАКОГО окна вовсе. Именно это и делает рез безопасным в каскаде.
live_b, eng_b, stop_b, cap_b, rows_b = drill(won=True)
_stop_eth = threading.Event()
live_b._windows["ETH"] = _stop_eth
live_b._windows["BTC"] = (_re_stop := threading.Event())
live_b.stats["wins"] = 0
live_b._cd_calls.clear()
live_b._cooldown_feed.clear()
_sv_i = ci._INDEX
ci._INDEX = live_b._idx_for_b1
try:
    # ЯРЛЫК ЧУЖОЙ (ETH), а план в кортеже — тот, под которым проба ушла (BTC).
    live_b._engine_timeout((LEG, ("ETH", MID, BORROWER, 90_000.0)), TXH)
finally:
    ci._INDEX = _sv_i
assert live_b.stats.get("wins") == 1, live_b.stats
assert not _stop_eth.is_set() and not _re_stop.is_set(), \
    ("рез #110: путь NO-RECEIPT не гасит НИ ОДНОГО окна — ни своего, ни соседского: cd=%s"
     % live_b._cd_calls)
assert live_b._cd_calls == [], \
    ("рез #110: и паузы не ставит ни одному фиду: %s" % live_b._cd_calls)
print("UNIT_OK: чужой ярлык не гасит ни соседа, ни «своё» окно — на этом пути решений по огню "
      "нет вовсе")

print("ПРОШЛИ: после реза ветка NO-RECEIPT считает ДЕНЬГИ (wins/noreceipt_win) и не управляет "
      "огнём; незнание не выдаётся ни за победу, ни за её отсутствие")
