"""ХВОСТ #110-х1 (ДЕНЬГИ): победа через NO-RECEIPT была НЕВИДИМА.

ЧТО БЫЛО. `_engine_timeout` спрашивает индекс первым и на ответе 'mined' книжит газ, спасает
склад ноги и считает позицию — но `fact['won']` не читал ВООБЩЕ. Значит первая в истории бота
победа, о которой узел квитанций промолчал (а он молчит регулярно: 889 «collect batch fail» за
сутки и ЛОЖНЫЕ NO-RECEIPT у замайненных проб — j1/j2), прошла бы мимо: ни `wins`, ни `disarm`,
ни WIN-кулдауна, ни закрытия окна. Движок продолжал бы жечь газ по УЖЕ ВЗЯТОЙ цели до конца
TTL окна, а суточный учёт победы не увидел бы.

ЧТО ТЕПЕРЬ. Вердикт победы один на все источники факта: `_probe_win`. Из ветки квитанции его
зовёт `_receipt_has_win`, из ветки NO-RECEIPT — `fact['won']` индекса (наш хэш среди логов
Liquidate у Morpho). Три значения `won` различаются: True — захват, False — ФАКТ отсутствия
(логи прочитаны), None — логи не читаны, судить не о чем, и огонь НЕ гасится.

НАСТОЯЩИЕ: `build_facts` и `parse_logs` (PURE-ядро индекса) на собранном блоке, `_ingest`,
`state`, `_engine_timeout`, `_probe_win`, `_set_cooldown`, настоящий `FireEngine.disarm/armed`.
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
    kill_verdict — МУТАЦИЯ «ветка mined не выносит вердикт победы» (редакция до р-6).
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
    # ЧЕЙ ЭТО ПЛАН (блокер B1 р-7): окно гасится ПО КЛЮЧУ ПЛАНА, а не по ярлыку фида в meta.
    # В бою ключ кладёт _prearm_board в момент публикации probe_board; здесь объект собран
    # руками, и без этой строки стенд мерил бы состояние, которого в бою не бывает.
    live._win_key["BTC"] = live.plan_key(MID, BORROWER)
    live._idx_for_b1 = idx          # тот же индекс нужен клетке 4 (повторный прогон вердикта)
    # СПИОН НА КУЛДАУНЕ: ставится ли WIN-пауза ТЕМ ЖЕ вызовом, что и на пути квитанции.
    # Взведётся ли она фактически — решает ручка #46 (SPEC_FIRE_COOLDOWN_TRIGGER), и это
    # решение владельца; путь NO-RECEIPT обязан отличаться от пути квитанции НИЧЕМ.
    cd_calls = []
    _real_cd = live._set_cooldown
    live._set_cooldown = lambda feed, src, proven=False: (
        cd_calls.append((feed, src, proven)) or _real_cd(feed, src, proven=proven))
    live._cd_calls = cd_calls
    if kill_verdict:
        live._probe_win = lambda *a, **k: None      # МУТАЦИЯ: вердикт победы не выносится

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
assert _e.armed() is True and not _s.is_set(), \
    "проба села, но приза не взяла — огонь гасить НЕ за что"
assert not _l.stats.get("wins"), _l.stats
print("UNIT_OK: ветка NO-RECEIPT+mined без победы (won=False — это ФАКТ, логи прочитаны) огонь "
      "не гасит и wins не растит")

# ---------------------------------------------------------------- 1. ПОБЕДА ЧЕРЕЗ NO-RECEIPT
live, eng, stop, cap, rows = drill(won=True)
assert live.stats.get("wins") == 1 and live.stats.get("noreceipt_win") == 1, \
    ("победа, увиденная ИНДЕКСОМ, обязана считаться победой: %s" % live.stats)
assert eng.armed() is False, \
    "приз взят — движок обязан быть РАЗОРУЖЁН немедленно, иначе газ жжётся по взятой цели"
assert stop.is_set(), "окно взятой цели обязано закрыться"
assert live._cd_calls == [("BTC", "probe", True)], \
    ("WIN-пауза обязана ставиться ТЕМ ЖЕ вызовом, что и на пути квитанции (взведётся ли она "
     "фактически — решает ручка #46): %s" % live._cd_calls)
_win_lines = [m for lv, m in cap.rows if "SPECFIRE WIN!!!" in m and "NO-RECEIPT" in m]
assert _win_lines and [lv for lv, m in cap.rows if "SPECFIRE WIN!!!" in m][0] >= logging.WARNING, \
    ("первая победа обязана быть СЛЫШНА и назвать источник вердикта: %s" % cap.rows[-3:])
print("UNIT_OK: победа через NO-RECEIPT ВИДНА: wins=1, noreceipt_win=1, движок разоружён, окно "
      "закрыто, пауза взведена, строка «%s»" % _win_lines[0][:80])

# ---------------------------------------------------------------- 2. НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД
live_u, eng_u, stop_u, cap_u, rows_u = drill(won=True, logs_read=False)
assert not live_u.stats.get("wins"), \
    ("won=None — это «логи не читаны», а не победа: объявить её значило бы погасить свой "
     "огонь посреди живой возможности: %s" % live_u.stats)
assert eng_u.armed() is True and not stop_u.is_set(), "по незнанию огонь не гасим"
# СЧЁТЧИК «ВЕРДИКТ НЕИЗВЕСТЕН» СНЯТ В Р-7 как структурно нулевой: здесь он «достижим» только
# потому, что дрель зовёт build_facts(logs=None) РУКАМИ, а боевой scan_block читает логи ПЕРВЫМ
# вызовом и на отказе бросает IndexUnavailable. Ноль в таком счётчике одинаково значил «вердикт
# всегда есть» и «ветка мертва». Требование осталось прежним — незнание обязано быть НАЗВАНО, —
# но названо оно теперь там, где его прочтёт человек: в строке самого исхода.
assert any("вердикт победы=None" in m for m in rows_u), \
    ("недоступность вердикта обязана быть НАЗВАНА в строке исхода: %s" % rows_u[-2:])
print("UNIT_OK: won=None (логи блока не читаны) победой НЕ считается и НАЗВАНО в строке исхода "
      "(«вердикт победы=None») — незнание закрывает гард, а не открывает его")

# ---------------------------------------------------------------- 3. МУТАЦИЯ: вернуть слепоту
live_m, eng_m, stop_m, cap_m, _rm = drill(won=True, kill_verdict=True)
assert not live_m.stats.get("wins") and eng_m.armed() is True and not stop_m.is_set(), \
    ("мутация «ветка mined не выносит вердикт победы» (редакция до р-6) ОБЯЗАНА краснить "
     "стенд — иначе тест не держит ничего: %s" % live_m.stats)
print("UNIT_OK: мутация (ветка mined слепа к fact['won'], как было) краснит стенд: победа "
      "невидима, движок остаётся армленным по УЖЕ ВЗЯТОЙ цели")

# ---------------------------------------------------------------- 4. B1: ЯРЛЫК != ПЛАН
# `FireEngine._target` — ОДНО перезаписываемое поле: ярлык ставит ПОСЛЕДНИЙ тикнувший
# keepalive любого окна каскада. Победа под планом окна BTC не имеет права гасить окно ETH.
live_b, eng_b, stop_b, cap_b, rows_b = drill(won=True)
_stop_eth = threading.Event()
live_b._windows["ETH"] = _stop_eth
live_b._win_key["ETH"] = live_b.plan_key("0x" + "ee" * 32, "0x" + "ef" * 20)
live_b._windows["BTC"] = (_re_stop := threading.Event())
live_b._win_key["BTC"] = live_b.plan_key(MID, BORROWER)
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
assert _re_stop.is_set(), \
    "окно ВЗЯТОЙ цели (его план == план пробы) обязано закрыться, даже если ярлык чужой"
assert not _stop_eth.is_set(), \
    ("окно СОСЕДА закрыто ЧУЖОЙ победой — его цель никто не брал, мы выключили живую "
     "возможность: cd=%s" % live_b._cd_calls)
assert live_b._cd_calls == [("BTC", "probe", True)], \
    ("WIN-пауза обязана лечь на фид ВЗЯТОЙ цели, а не на ярлык последнего тика: %s"
     % live_b._cd_calls)
print("UNIT_OK: B1 — победа под чужим ярлыком гасит окно ПЛАНА (BTC) и не трогает соседа (ETH)")

print("ПРОШЛИ: вердикт победы один на все источники факта — квитанция и индекс дают ОДНО И ТО "
      "ЖЕ действие, а незнание не выдаётся ни за победу, ни за её отсутствие")
