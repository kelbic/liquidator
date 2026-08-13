"""МАЙОР E (ревью р-4): накопитель `-32601` обязан РАЗРЯЖАТЬСЯ — успехом и временем.

ЧТО БЫЛО. `_rcpt_unsup` был НАБОРОМ номеров блоков и чистился ТОЛЬКО в `_rcpt_unlatch`, то есть
K=3 отказов копились ЗА ВСЮ ЖИЗНЬ ПРОЦЕССА. За адресом вроде `base-rpc.publicnode.com` стоит
ПУЛ бэкендов, и редкий чужой ответ `-32601` неотличим от свойства узла: три таких ответа,
разнесённые на часы, гасили способность к квитанциям на 6ч и печатали про УЗЕЛ «не поддержан».
Ценой был ноль в `index_hits`/`polls_saved` — ГЛАВНОМ приёмном числе пакета (замер дрелью
d1b: при p=0.5% способность погашена 97.1% модельных суток).

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ (на НАСТОЯЩЕМ `scan_block`, денежный путь, блок с нашей tx):
  1. чередование отказ-успех-отказ-успех НЕ гасит способность (успех разряжает);
  2. узел, который метода правда не умеет, — гасится (латч жив, иначе правка бесполезна);
  3. момент латча на ДЕНЕЖНОМ пути звучит WARNING и несёт «K=3 на РАЗНЫХ блоках» — приёмка (в)
     читает «квитанции=не поддержан» как свойство узла ТОЛЬКО вместе с этой строкой;
  4. отказы старше скользящего окна выпадают сами (K обязаны сойтись близко по времени).

Подменён РОВНО ОДИН шов — сокет (`http`). `scan_block`, `build_facts`, `_note_unsupported`,
`_note_receipts_ok`, `_prune_unsup`, `receipts_for` исполняются настоящие.
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402

BN0 = 49890266
OURS = "0x" + "ab" * 20
TXH = "cd" * 32
LOGGER = logging.getLogger("liquidator")


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, rec):
        self.rows.append((rec.levelno, rec.getMessage()))


def run(pattern, step=2.0):
    """pattern: 'x' — узел ответил -32601, 'o' — вернул список квитанций."""
    T = [1000.0]
    n = {"i": 0}

    def _http(url, payload, timeout):
        m = payload["method"]
        if m == "eth_blockNumber":
            return {"result": hex(BN0 + 10_000)}
        if m == "eth_getLogs":
            return {"result": []}
        if m == "eth_getBlockByNumber":
            bn = int(payload["params"][0], 16)
            return {"result": {"number": hex(bn), "baseFeePerGas": hex(10),
                               "transactions": [{"hash": "0x" + TXH, "to": OURS,
                                                 "transactionIndex": hex(7),
                                                 "maxPriorityFeePerGas": hex(2_000_000),
                                                 "type": "0x2"}]}}
        if m == "eth_getBlockReceipts":
            i = n["i"]
            n["i"] += 1
            if i < len(pattern) and pattern[i] == "x":
                return {"error": {"code": -32601, "message": "rpc method is unsupported"}}
            return {"result": [{"transactionHash": "0x" + TXH, "gasUsed": hex(21000),
                                "effectiveGasPrice": hex(1_000_000), "status": "0x1",
                                "transactionIndex": hex(7), "blockNumber": hex(BN0)}]}
        return {"result": None}

    idx = ci.ChainIndex("https://node.test/KEY", watch_to=[OURS], http=_http,
                        now=lambda: T[0], sleep=lambda s: None)
    cap = Cap()
    cap.setLevel(logging.DEBUG)
    LOGGER.addHandler(cap)
    _lvl = LOGGER.level
    LOGGER.setLevel(logging.INFO)
    try:
        for k in range(len(pattern)):
            T[0] += step
            idx.note_hash(TXH, head=BN0 + k - 1)
            idx.scan_block(BN0 + k)
    finally:
        LOGGER.setLevel(_lvl)
        LOGGER.removeHandler(cap)
    latch = [(lv, m) for lv, m in cap.rows if "не умеет eth_getBlockReceipts" in m]
    return idx, cap, latch


# ---------------------------------------------------------------- 1. ПУЛ: успех разряжает
idx1, _c1, latch1 = run("xoxoxoxoxo")
assert idx1._receipts_ok is True, "успешный ответ списком обязан РАЗРЯЖАТЬ накопитель -32601"
assert not latch1, latch1
assert idx1._rcpt_probe != "не поддержан", idx1._rcpt_probe
assert len(idx1.receipts_for([TXH])) == 1, "квитанции обязаны работать: index_hits достижим"
print("UNIT_OK: редкий -32601 от ПУЛА не гасит способность — успех списком разряжает "
      "накопитель (было: три ответа за всю жизнь процесса = 6ч немоты и ложное «узел не "
      "поддержан», обнулявшее index_hits/polls_saved)")

# ---------------------------------------------------------------- 2. латч ЖИВ (иначе правка пуста)
idx2, _c2, latch2 = run("xxxxx")
assert idx2._receipts_ok is False and idx2._rcpt_probe == "не поддержан", idx2._rcpt_probe
assert len(latch2) == 1, latch2
_lv, _msg = latch2[0]
assert _lv >= logging.WARNING, ("латч на ДЕНЕЖНОМ пути обязан быть слышен: он обнуляет "
                                "index_hits, а `self._log` по умолчанию пишет INFO", _lv)
assert ("K=%d на РАЗНЫХ блоках" % ci.CHAIN_INDEX_RCPT_UNSUP_K) in _msg, _msg
print("UNIT_OK: узел, который метода НЕ УМЕЕТ, по-прежнему гасится — и момент латча звучит "
      "WARNING со словами «K=%d на РАЗНЫХ блоках» (приёмка (в) читает «не поддержан» как "
      "свойство узла только вместе с этой строкой)" % ci.CHAIN_INDEX_RCPT_UNSUP_K)

# ---------------------------------------------------------------- 3. окно скользящее
idx3, _c3, latch3 = run("xxx", step=ci.CHAIN_INDEX_RCPT_UNSUP_WIN_SEC + 10.0)
assert idx3._receipts_ok is True, ("три отказа, разнесённые ШИРЕ окна, свойством узла не "
                                   "являются — окно обязано выбрасывать старые")
assert not latch3, latch3
print("UNIT_OK: K отказов обязаны сойтись в СКОЛЬЗЯЩЕМ окне (%.0fс) — разнесённые шире окна "
      "способность не гасят" % ci.CHAIN_INDEX_RCPT_UNSUP_WIN_SEC)

# ---------------------------------------------------------------- 4. МУТАЦИЯ: снять разряд
_sv = ci.ChainIndex._note_receipts_ok
try:
    ci.ChainIndex._note_receipts_ok = lambda self, n=None: None      # прежнее поведение
    idxm, _cm, latchm = run("xoxoxoxoxo")
    assert idxm._receipts_ok is False and latchm, \
        "мутация «успех не разряжает» ОБЯЗАНА краснить стенд — иначе тест ничего не держит"
finally:
    ci.ChainIndex._note_receipts_ok = _sv
print("UNIT_OK: мутация (успех НЕ разряжает — редакция до р-5) краснит стенд: способность "
      "гаснет на том же чередовании отказ-успех")

print("ПРОШЛИ: способность узла к квитанциям — свойство, проверяемое ВО ВРЕМЕНИ, а не сумма "
      "отказов за жизнь процесса")
