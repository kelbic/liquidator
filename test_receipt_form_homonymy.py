"""ОМОНИМИЯ ФОРМЫ КВИТАНЦИИ (вскрытие 26.08: `collect handler fail`, 4905 падений за 6ч).

Класс дефекта: одно поле приходит В ДВУХ ФОРМАХ по двум путям, а код знает только одну.
Легаси-тред читает квитанцию через web3 -> `gasUsed` = int. Коллектор окна читает партию
сырым JSON-RPC -> то же поле = hex-строка '0x1c5b3'. `int()` без base на второй бросает
ValueError ВНУТРИ `_res_from_receipt`, то есть раньше, чем исход успевает записаться.

Цена дефекта в бою: в режиме shadow гибнет теневая СВЕРКА — прибор, ради которого П.3 и
строился (решение «переключать ли деньги на коллектор» принимать не на чем). Если бы
режим уже вёл деньги, гибли бы вердикт победы по квитанции и снятие брони #93.

Приёмка:
(а) ЭТАЛОН: обе формы дают ОДИН И ТОТ ЖЕ кортеж исхода (web3-форма — эталон);
(б) позиция: «поля нет» остаётся None и НЕ превращается в 0 (ноль — законный индекс);
(в) мусор НЕ глотается молча (незнание не выдаёт себя за нулевой газ);
(г) КРАСНЫЙ-НА-БАЗЕ: прежняя формула на hex-форме обязана падать — иначе тест не меряет.
"""
import sys
import pathlib as _pl
from types import SimpleNamespace

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402

o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o.cfg = SimpleNamespace(eth_price_usd=4000.0, liquidator_address="0xliq")

# Одна и та же квитанция в двух формах: gasUsed=116147, gasPrice=1000, блок 50481469, idx 164
W3 = {"status": 1, "gasUsed": 116147, "effectiveGasPrice": 1000,
      "blockNumber": 50481469, "transactionIndex": 164, "logs": []}
RAW = {"status": "0x1", "gasUsed": "0x1c5b3", "effectiveGasPrice": "0x3e8",
       "blockNumber": "0x302493d", "transactionIndex": "0xa4", "logs": []}

# ---- (а) ЭТАЛОН: формы совпадают ----
res_w3 = o._res_from_receipt(W3)
try:
    res_raw = o._res_from_receipt(RAW)
    ok_raw = True
except Exception as e:                                       # noqa: BLE001
    res_raw, ok_raw = repr(e), False
check("сырая JSON-форма (hex) больше НЕ роняет разбор исхода", ok_raw, res_raw)
check("(а) ЭТАЛОН: hex-форма даёт ТОТ ЖЕ кортеж, что web3-форма",
      ok_raw and res_raw == res_w3, (res_w3, res_raw))
check("газ посчитан по обеим формам одинаково и не ноль",
      ok_raw and res_raw[1] == res_w3[1] and res_w3[1] > 0, (res_w3[1],))
check("позиция разобрана из hex: блок 50481469, индекс 164",
      ok_raw and res_raw[2] == 50481469 and res_raw[3] == 164, res_raw[2:4])

# ---- (б) «поля нет» != 0 ----
_no_pos = dict(RAW)
_no_pos.pop("blockNumber")
_no_pos.pop("transactionIndex")
r2 = o._res_from_receipt(_no_pos)
check("(б) отсутствие позиции -> None, а НЕ 0 (незнание не выдаёт себя за измерение)",
      r2[2] is None and r2[3] is None, r2[2:4])
_zero_ix = dict(RAW, transactionIndex="0x0")
check("(б2) законный НОЛЬ индекса остаётся нулём и не путается с None",
      o._res_from_receipt(_zero_ix)[3] == 0, o._res_from_receipt(_zero_ix)[3])
check("статус '0x0' (реверт) читается как 0, а не как отсутствие",
      o._res_from_receipt(dict(RAW, status="0x0"))[0] == 0)

# ---- (в) мусор: конвенция принята ОБЩЕЙ, не изобретаем свою ----
# Разбор идёт существующим chain_index.rcpt_int (fail-open: нечитаемое -> default).
# Для газа это named limit: мусор станет «нулевым газом». Конвенция общая для обоих
# источников (см. receipt_like в chain_index) — тест ФИКСИРУЕТ её, а не одобряет.
r3 = o._res_from_receipt(dict(RAW, gasUsed="не-число"))
check("(в) мусорный газ уходит в default=0 по ОБЩЕЙ конвенции rcpt_int (named limit)",
      r3[1] == 0.0 and r3[0] == 1, (r3[0], r3[1]))

# ---- (г) КРАСНЫЙ-НА-БАЗЕ: прежняя формула обязана падать на hex ----
def _legacy(r):
    """Формула ДО фикса, дословно."""
    gas_usd = (int(r.get("gasUsed") or 0) * int(r.get("effectiveGasPrice") or 0)
               * 4000.0 / 1e18)
    return (int(r.get("status") or 0), gas_usd, r.get("blockNumber"),
            r.get("transactionIndex"))


try:
    _legacy(RAW)
    _base_red = False
except ValueError:
    _base_red = True
check("(г) КРАСНЫЙ-НА-БАЗЕ: прежняя формула на hex-форме падает ValueError", _base_red,
      "старая формула НЕ упала — тест не меряет тот дефект")
check("(г2) прежняя формула на web3-форме работала — дефект именно в ФОРМЕ, не в математике",
      _legacy(W3)[:2] == res_w3[:2], (_legacy(W3)[:2], res_w3[:2]))

# ---- гарды дрейфа ----
_S = open("chain/spec_fire_live.py").read()
import re as _re
_res_src = _re.search(r"def _res_from_receipt.*?def ", _S, _re.S).group(0)
check("в _res_from_receipt голого int(r.get( не осталось (весь разбор через rcpt_int)",
      "int(r.get(" not in _res_src.replace("rcpt_int(r.get(", ""), _res_src[-200:])
check("разбор ЕДИНЫМ хелпером chain_index.rcpt_int, а не своим дубликатом",
      "from chain.chain_index import rcpt_int" in _res_src and "def _rcpt_int" not in _S)
check("причина записана в коде (две формы, 4905 падений)",
      "4905 падений" in _S and "'0x1c5b3'" in _S)
check("позиция с default=None (незнание не притворяется нулём)",
      'rcpt_int(r.get("blockNumber"), None)' in _S
      and 'rcpt_int(r.get("transactionIndex"), None)' in _S)
from chain.chain_index import rcpt_int as _ri  # noqa: E402
check("единый парсер разбирает обе формы", _ri("0x1c5b3", 0) == 116147
      and _ri(116147, 0) == 116147 and _ri(None, None) is None)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: обе формы квитанции дают один исход; позиция не теряет None; "
                     "мусор не глотается"))
sys.exit(1 if FAILS else 0)
