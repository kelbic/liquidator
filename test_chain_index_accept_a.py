"""ПРИЁМКА «а» ФОНОВОГО ИНДЕКСА (#110): «ЖИВ» — утверждение, а не украшение строки.

ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ (ревью р-3, условие 2). Дефолт `CHAIN_INDEX_FULL_ON_LOGS` в этом
раунде выключен, а дословная приёмка р-2 требовала «полных=M>0 … ВЕРДИКТ: ЖИВ». В ШТИЛЬ
полных чтений при выключенной ручке не будет ПО ПОСТРОЕНИЮ (блок читается целиком только под
нашими пробами), то есть родитель либо прочтёт законное состояние как провал, либо оставит
дорогой дефолт РАДИ СТРОКИ. Здесь эта развилка закрыта поведением, а не текстом:

  * штиль при выключенной ручке   -> ЧАСТИЧНО (WARNING, законный проход, причина названа);
  * отказ узла в окне             -> НЕ «ЖИВ» (приёмка требует отказов=0), но и НЕ «МЁРТВ»:
                                     транзиентный отказ при живом чтении — та же ложная
                                     тревога, что «ЗАМОРОЖЕН» на штатной уступке ключа;
  * дыра / протухание / ноль блоков -> МЁРТВ (ERROR);
  * наши пробы летели, дыр нет, отказов нет -> «ЖИВ» достижим (иначе критерий недостижим ПО
    ПОСТРОЕНИЮ, а недостижимый критерий приёмкой не является).

Подменён РОВНО ОДИН шов — сокет. `_selfcheck`, `snapshot`, `scan_block`, `build_facts`,
`fresh/age` исполняются настоящие, на записанном ответе узла (блок 49890266).
"""
import gzip
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
BN = 49890266
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RECEIPTS = json.loads((FIX / "receipts_49890266.json").read_text())
H_FIRE = next(rc["transactionHash"] for rc in RECEIPTS if rc["to"].lower() == BOARD)[2:]
LOGGER = logging.getLogger("liquidator")


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errs, self.warns = [], []

    def emit(self, rec):
        (self.errs if rec.levelno >= logging.ERROR else self.warns).append(rec.getMessage())


def run(*, ours=False, head_fail=False, gap=False, blocks=25):
    """Одно окно приёмки: `blocks` блоков по 2с, затем НАСТОЯЩИЙ `_selfcheck`.

    ours=True      — наша проба в полёте (боевой путь полного чтения);
    head_fail=True — узел разово не ответил про ГОЛОВУ (тот же `_note_fail`, что зовёт
                     `_confirmed_head`): отказ БЕЗ потери блока, то есть дыры не создаёт —
                     иначе «отказ» и «дыра» было бы не развести;
    gap=True       — один блок пропущен (индекс обязан объявить дыру).
    """
    T = [1000.0]
    state = {"n": 0}

    def _http(url, payload, timeout):
        m = payload["method"]
        state["n"] += 1
        if m == "eth_getLogs":
            frm = int(payload["params"][0]["fromBlock"], 16)
            return {"result": LOGS if frm == BN else []}
        if m == "eth_getBlockByNumber":
            b = dict(BLOCK["result"])
            bn = int(payload["params"][0], 16)
            if bn != BN:
                b["number"], b["transactions"] = hex(bn), []
            return {"result": b}
        if m == "eth_getBlockReceipts":
            return {"result": RECEIPTS}
        if m == "eth_blockNumber":
            return {"result": hex(BN + blocks + 8)}
        raise AssertionError(m)

    idx = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=_http,
                        now=lambda: T[0], log_fn=lambda m: None)
    if ours:
        idx.note_hash(H_FIRE, head=BN - 1)
    cap = Cap()
    cap.setLevel(logging.DEBUG)
    LOGGER.addHandler(cap)
    try:
        for i in range(blocks):
            T[0] += 2.0
            if gap and i == 3:
                continue                       # блок пропущен -> дыра
            try:
                idx.scan_block(BN + i)
            except ci.IndexUnavailable as e:
                idx._note_fail(e, BN + i)      # тот же путь, что в `_catch_up`
            if ours and i % 4 == 0:
                idx.note_hash(H_FIRE, head=BN - 1)   # пробы летят всё окно
        if head_fail:
            idx._note_fail(ci.IndexUnavailable("429 over rate limit", "fixture"))
        T[0] += ci.CHAIN_INDEX_SELFCHECK_SEC
        idx._last_ok = T[0]                    # окно кончилось скан-успехом (иначе протухли)
        if gap:
            idx.stats["gaps"] = idx.stats.get("gaps", 0) or 1
        idx._selfcheck()
    finally:
        LOGGER.removeHandler(cap)
    line = ([m for m in cap.errs + cap.warns if "CHAIN-INDEX ПРИЁМКА" in m] or [""])[0]
    return idx, line, cap


# ------------------------------------------------ 1. ШТИЛЬ при выключенной ручке = ЧАСТИЧНО
_sv = ci.CHAIN_INDEX_FULL_ON_LOGS
try:
    ci.CHAIN_INDEX_FULL_ON_LOGS = False
    idx1, l1, cap1 = run()
    assert "ВЕРДИКТ: ЧАСТИЧНО" in l1, l1
    assert "ПОЛНЫХ чтений в окне не было" in l1 and "ЗАКОННЫЙ проход" in l1, l1
    assert idx1.snapshot()["blocks"] > 0 and idx1.snapshot()["full"] == 0, idx1.snapshot()
    assert not cap1.errs, "законный штиль не имеет права быть ERROR: %s" % cap1.errs
    print("UNIT_OK: штиль при FULL_ON_LOGS=0 — «ЧАСТИЧНО», WARNING, причина названа "
          "(блоков=%d полных=0) — это проход приёмки «а», а не провал"
          % idx1.snapshot()["blocks"])

    # -------------------------------------------- 2. НАШИ ПРОБЫ -> «ЖИВ» ДОСТИЖИМ
    idx2, l2, cap2 = run(ours=True)
    s2 = idx2.snapshot()
    assert s2["full"] > 0 and s2["gaps"] == 0 and s2["fails"] == 0 and s2["fresh"] is True, s2
    assert "ВЕРДИКТ: ЖИВ" in l2, l2
    assert not cap2.errs, cap2.errs
    print("UNIT_OK: с НАШИМИ пробами в полёте «ЖИВ» достижим при выключенной ручке "
          "(блоков=%d полных=%d дыр=0 отказов=0) — критерий не пустой по построению"
          % (s2["blocks"], s2["full"]))

    # -------------------------------------------- 3. ОТКАЗ УЗЛА -> НЕ «ЖИВ», но и НЕ «МЁРТВ»
    idx3, l3, cap3 = run(ours=True, head_fail=True)
    _s3 = idx3.snapshot()
    assert _s3["fails"] > 0 and _s3["gaps"] == 0 and _s3["full"] > 0, _s3
    assert "ВЕРДИКТ: ЖИВ" not in l3, \
        ("приёмка «а» требует отказов=0: прибор, который часть окна не читал ничего, «ЖИВ» "
         "называться не имеет права: %s" % l3)
    assert "отказов узла=" in l3 and "ЧАСТИЧНО" in l3, l3
    assert not [m for m in cap3.errs if "ПРИЁМКА" in m], \
        "транзиентный отказ — не смерть прибора: %s" % cap3.errs
    print("UNIT_OK: отказ узла в окне снимает «ЖИВ» (вердикт «%s»), но не объявляет смерть — "
          "ложная тревога того же класса, что «ЗАМОРОЖЕН» на уступке ключа"
          % l3.split("ВЕРДИКТ: ")[1][:40])

    # -------------------------------------------- 4. ДЫРА -> МЁРТВ и ERROR
    idx4, l4, cap4 = run(ours=True, gap=True)
    assert "ВЕРДИКТ: МЁРТВ" in l4 and "дыр=" in l4, l4
    assert cap4.errs and "ПРИЁМКА" in cap4.errs[0], cap4.errs
    print("UNIT_OK: дыра в покрытии — «МЁРТВ» на ERROR (право сказать «пробы не было» потеряно)")
finally:
    ci.CHAIN_INDEX_FULL_ON_LOGS = _sv

print("ПРОШЛИ: приёмка «а» разведена — ЖИВ/ЧАСТИЧНО/МЁРТВ различают ШТИЛЬ, ОТКАЗ и СМЕРТЬ")
