"""#110 ФОНОВЫЙ ИНДЕКС ЦЕПИ — позитивный контроль на НАСТОЯЩЕМ ответе узла + гарды незнания.

ПОЧЕМУ ТЕСТ ВЫГЛЯДИТ ИМЕННО ТАК. У нас есть задокументированный класс дефекта «шов подменён
ЭТАЖОМ ВЫШЕ дефекта» (38 зелёных тестов при 100% мёртвом боевом пути) и класс «тест, который
может вернуть только ноль». Поэтому здесь:

  * подменяется РОВНО ОДИН стык — `http` (сокет). Всё остальное исполняется настоящим:
    `ChainIndex.scan_block`, `build_facts`, `parse_block`, `parse_logs`, `_merge_receipts`,
    учёт покрытия, `state()`, `corpses()`;
  * ответы узла — записанные СЫРЫЕ (`fixtures/`, см. README там): блок 49890266 целиком
    (1175 tx), его логи, настоящие квитанции;
  * ПОЗИТИВНЫЙ КОНТРОЛЬ известного факта: в этом блоке ТОЧНО есть 33 наших пробы к доске
    0x85bade2c, транзит на 1083 с tip 2 000 000 и победитель на 1084 с тем же tip. Если бы
    парсер молча возвращал пустоту, весь остальной набор проверок («трупов нет», «наших нет»)
    оставался бы зелёным — именно на этом мы уже горели;
  * ПОЗИТИВНЫЙ КОНТРОЛЬ ВЕРДИКТА ПОБЕДЫ: наших побед в цепи нет ни одной, поэтому механизм
    проверяется на ЧУЖОЙ победе — контракт победителя объявляется наблюдаемым, и индекс обязан
    сказать won=True ему и won=False нашим 33 пробам В ТОМ ЖЕ блоке. Так знаменатель у
    вердикта не пустой.
"""
import gzip
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
BN = 49890266
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
WINNER_CONTRACT = "0x81c8654d29b54828c9fb75db3cb291a7c9fcf6ae"   # чужая стена, взявшая приз

BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RECEIPTS = json.loads((FIX / "receipts_49890266.json").read_text())


def make_http(calls, *, receipts=True, fail=None, head=BN):
    """Сокет-двойник: отдаёт ЗАПИСАННЫЕ ответы узла. Ничего не разбирает и не решает."""
    def _http(url, payload, timeout):
        m = payload["method"]
        calls.append(m)
        if fail is not None and m in fail:
            raise ci.IndexUnavailable(fail[m], "fixture")
        if m == "eth_getLogs":
            frm = int(payload["params"][0]["fromBlock"], 16)
            return {"result": LOGS if frm == BN else []}
        if m == "eth_getBlockByNumber":
            bn = int(payload["params"][0], 16)
            if bn != BN:
                # другой блок: тот же конверт, но без tx — узел так и отвечает на пустой блок
                b = dict(BLOCK["result"])
                b["number"] = hex(bn)
                b["transactions"] = []
                return {"result": b}
            return BLOCK
        if m == "eth_getBlockReceipts":
            if not receipts:
                return {"error": {"code": -32601, "message": "rpc method is unsupported"}}
            return {"result": RECEIPTS}
        if m == "eth_blockNumber":
            # ГОЛОВА УЗЛА — АВТОРИТЕТ. Двойник умеет отдавать её динамически (head может быть
            # вызываемым): подача из flashblocks несёт pending-номер, и индекс обязан
            # сканировать только до того, что узел ПРИЗНАЁТ.
            return {"result": hex(head() if callable(head) else head)}
        raise AssertionError("индекс не имеет права звать %s" % m)
    return _http


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- 1. позитивный контроль
calls = []
clk = Clock()
idx = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls),
                    now=clk, log_fn=lambda m: None)
facts = idx.scan_block(BN)

assert len(facts["ours"]) == 33, "ЗНАЛИ ЗАРАНЕЕ: в блоке 33 наши пробы к доске, найдено %d" % len(facts["ours"])
tips = {}
for f in facts["ours"].values():
    tips[f["tip"]] = tips.get(f["tip"], 0) + 1
assert tips == {1000000: 6, 1100000: 15, 1775000: 6, 3000000: 6}, tips
_idx = sorted(f["index"] for f in facts["ours"].values())
assert _idx[0] == 17 and _idx[-1] == 1146, _idx[:3]
assert facts["ntx"] == 1175 and facts["base_fee"] == 5088466
print("UNIT_OK: индекс ВИДИТ известные 33 пробы в блоке %d (позиции 17..1146, четыре тира tip)" % BN)

assert len(idx.transits_of(BN)) == 2, "в блоке ДВА транзита (406 и 1083) — их и обязано быть два"
tr = idx.transit_of(BN)
assert tr is not None, "транзит оракула обязан быть найден по логу AnswerUpdated"
assert tr[0] == 1083, ("призовой транзит — тот, за которым стоит победитель, а не первый в "
                       "блоке; найдено %s" % (tr,))
assert tr[1] == 2000000, "tip транзита 2000000 wei, найдено %s" % (tr,)
assert tr[2] == 37, "полоса ничьей на этом tip — 37 транзакций, насчитано %s" % (tr[2],)
wins = idx.winners_of(BN)
assert len(wins) == 1 and wins[0]["index"] == 1084, wins
assert wins[0]["tip"] == tr[1], "механика приза: победитель несёт tip транзита до wei"
assert wins[0]["index"] == tr[0] + 1, "победитель стоит на транзит+1"
print("UNIT_OK: транзит idx=1083 tip=2e6 (ничья 37), победитель idx=1084 с тем же tip — "
      "механика приза читается ИЗ ИНДЕКСА, а не из офлайн-разбора")

# метрика №1 снимается прямо здесь: ни одна проба не попала ни в полосу, ни в tip
rep = idx.our_position_report(BN)
assert "tip-match=0" in rep and "в полосе [T+1,T+20]=0" in rep, rep
print("UNIT_OK: отчёт позиций — %s" % rep)

# ПРИЁМКА (б): строка позиции с транзитом СВОЕГО фида. Без agg строка считалась бы против
# транзита кого угодно — в блоке без Liquidate это и происходило.
_our_agg = idx.transits_of(BN)[1]["agg"]
rep_agg = idx.our_position_report(BN, agg=_our_agg)
assert "транзит idx=1083" in rep_agg and "agg=%s" % _our_agg[:10] in rep_agg, rep_agg
assert "полоса вперёд 37" in rep_agg and "вся полоса 85" in rep_agg, \
    ("полоса ВПЕРЁД (приз) и ВСЯ полоса — разные величины и обязаны быть разведены: %s" % rep_agg)
rep_alien = idx.our_position_report(BN, agg="0x" + "de" * 20)
assert "НАШЕГО фида" in rep_alien, ("транзит ЧУЖОГО фида не имеет права выдаваться за наш: %s"
                                    % rep_alien)
assert idx.transit_report(BN, agg="0x" + "de" * 20)[0] == "foreign"
print("UNIT_OK: строка позиции считается против транзита СВОЕГО фида (agg), чужой транзит даёт "
      "исход 'foreign', а не подменяет собой наш")

# ---------------------------------------------------------------- 2. вердикт победы
calls2 = []
idx2 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD, WINNER_CONTRACT],
                     http=make_http(calls2), now=Clock(), log_fn=lambda m: None)
f2 = idx2.scan_block(BN)
won = [h for h, f in f2["ours"].items() if f["won"]]
assert len(won) == 1, "чужая победа в этом блоке ровно одна, найдено %d" % len(won)
assert f2["ours"][won[0]]["index"] == 1084
_probes = [f for f in f2["ours"].values() if f["to"] == BOARD]
assert len(_probes) == 33 and all(f["won"] is False for f in _probes), \
    ("наши 33 пробы обязаны получить ФАКТ won=False (а не None): победа эмитит Liquidate, "
     "и его отсутствие — утверждение, а не молчание")
_wall = [f for f in f2["ours"].values() if f["to"] == WINNER_CONTRACT]
assert len(_wall) > 1, "стена шлёт в блок много tx — знаменатель у вердикта не единичный (%d)" % len(_wall)
print("UNIT_OK: вердикт победы работает на ЧУЖОЙ победе (знаменатель не пуст): 1 won=True из "
      "%d tx стены, 33 наши пробы won=False в том же блоке" % len(_wall))

# ---------------------------------------------------------------- 3. трупы (#108)
pairs = idx.corpses()
assert pairs is not None and len(pairs) == 1, pairs
(mid, borrower), (repaid, bad) = list(pairs.items())[0]
assert borrower.startswith("0x") and len(borrower) == 42 and repaid > 0, (borrower, repaid)
assert bad == 0, "в этом событии bad debt нулевой"
print("UNIT_OK: труп из лога Liquidate: рынок %s… заёмщик %s… repaid_shares=%d" %
      (mid[:10], borrower[:10], repaid))

# ---------------------------------------------------------------- 4. квитанции блока
ours_h = [h for h in facts["ours"]]
r = idx.receipts_for(ours_h)
assert len(r) == 2, "квитанции пришли на 2 хэша из 33 — столько их в фикстуре, %d" % len(r)
one = r[[h for h in ours_h if h in r][0]]
# ФОРМА КВИТАНЦИИ ОДНА НА ОБА ИСТОЧНИКА: hex-строки, как отдаёт узел. Прежде индекс отдавал
# числа, сеть — hex, и денежный путь молча падал на одном из двух (см. test_engine_result_fmt).
assert one["status"] == "0x1" and one["gasUsed"] == hex(54710) and one["logs"] == []
assert ci.rcpt_int(one["status"]) == 1 and ci.rcpt_int(one["gasUsed"]) == 54710
assert ci.rcpt_int(one["transactionIndex"]) in (17, 163) and ci.rcpt_int(one["blockNumber"]) == BN
# effectiveGasPrice БЕЗ квитанции: base + min(tip, maxFee-base). Сверяем с НАСТОЯЩИМ полем узла.
for rc in RECEIPTS:
    h = rc["transactionHash"].lower()
    f = facts["ours"].get(h) or f2["ours"].get(h)
    if f is None:
        continue
    assert f["eff_gas_price"] == int(rc["effectiveGasPrice"], 16), \
        "формула effectiveGasPrice разошлась с квитанцией узла: %s vs %s" % (
            f["eff_gas_price"], rc["effectiveGasPrice"])
print("UNIT_OK: газ и статус склеены из квитанций блока; формула effectiveGasPrice сверена с "
      "НАСТОЯЩИМ полем узла на 3/3 tx")
assert len([c for c in calls if c == "eth_getTransactionReceipt"]) == 0, \
    "индекс не имеет права спрашивать квитанции поштучно — ради этого он и заведён"
assert calls == ["eth_getLogs", "eth_getBlockByNumber", "eth_getBlockReceipts"], calls
print("UNIT_OK: цена блока — ТРИ запроса (логи, блок, квитанции блока) против 33 поштучных")

# ---------------------------------------------------------------- 5. узел не умеет getBlockReceipts
calls3 = []
idx3 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD],
                     http=make_http(calls3, receipts=False), now=Clock(), log_fn=lambda m: None)
idx3.scan_block(BN)
idx3.scan_block(BN + 1)
assert calls3.count("eth_getBlockReceipts") == 1, \
    "способность узла выясняется ОДИН раз, повторов быть не должно: %s" % calls3
assert idx3._receipts_ok is False
assert len(idx3.receipts_for(list(facts["ours"]))) == 0, \
    "без gasUsed квитанцию выдавать НЕЛЬЗЯ — денежный учёт по выдуманному числу это вторая ложь"
assert idx3.lookup(list(facts["ours"])[0]) is not None, "позиции при этом остаются известны"
print("UNIT_OK: узел без eth_getBlockReceipts — способность гасится навсегда, позиции живут, "
      "газ НЕ выдумывается")

# ---------------------------------------------------------------- 6. три исхода state()
h_known = list(facts["ours"])[0]
h_ghost = "0x" + "ee" * 32
clk.t += 1.0
idx.note_hash(h_ghost, head=BN)
st, fact = idx.state(h_known, None)
assert st == "mined" and fact["index"] == facts["ours"][h_known]["index"], st
st, why = idx.state(h_ghost)
assert st == "unknown" and "окно наблюдения" in why, \
    "приговор ДО закрытия окна наблюдения запрещён: %s" % (why,)
for i in range(1, ci.CHAIN_INDEX_ABSENT_BLOCKS + 1):
    idx.scan_block(BN + i)                       # ждём свой хэш => блоки читаются ЦЕЛИКОМ
st, why = idx.state(h_ghost)
assert st == "absent", ("%d блоков подряд прочитаны ЦЕЛИКОМ и хэша там нет — это приговор, "
                        "а не незнание: %s" % (ci.CHAIN_INDEX_ABSENT_BLOCKS, why))
st, why = idx.state("0x" + "ff" * 32)
assert st == "unknown" and "не регистрировался" in why, why
clk.t += 999.0                                   # индекс протух
st, why = idx.state(h_ghost)
assert st == "unknown" and "протух" in why, (st, why)
print("UNIT_OK: три исхода различимы — mined / absent / unknown; протухший индекс НЕ выносит "
      "приговор (незнание закрывает гард)")

# ---------------------------------------------------------------- 7. дыра рвёт право на приговор
calls4 = []
clk4 = Clock()
idx4 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls4),
                     now=clk4, log_fn=lambda m: None)
idx4.note_hash(h_ghost, head=BN - 20)
for i in (20, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6):   # 19 пропущен => ДЫРА
    idx4.scan_block(BN - i)
st, why = idx4.state(h_ghost)
assert st == "unknown" and str(BN - 19) in why and idx4.stats["gaps"] == 1, \
    (st, why, idx4.stats)
print("UNIT_OK: пропущенный блок = дыра, и она ОТНИМАЕТ право сказать «пробы не было»")

# ---------------------------------------------------------------- 8. лёгкое чтение честно себя объявляет
calls5 = []
clk5 = Clock()
idx5 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls5),
                     now=clk5, log_fn=lambda m: None)
idx5.scan_block(BN + 5)                           # логов нет, хэшей не ждём => блок не читаем
assert calls5 == ["eth_getLogs"], "тихий блок обязан стоить ОДИН запрос: %s" % calls5
idx5.note_hash(h_ghost, head=BN + 5)
# ОКНО НАЧИНАЕТСЯ С from_bn+1. Блок BN+5 был ЗАПЕЧАТАН на момент отправки (и прочитан легко
# ДО регистрации хэша) — наша tx в нём оказаться не могла, и требовать его покрытия значило бы
# не выдать «absent» НИКОГДА (счётчик noreceipt_dropped_confirmed был структурно нулевым).
for i in range(6, 6 + ci.CHAIN_INDEX_ABSENT_BLOCKS + 1):
    idx5.scan_block(BN + i)
assert "eth_getBlockByNumber" in calls5, "ждём свой хэш => блоки читаются ПОЛНОСТЬЮ"
st, why = idx5.state(h_ghost)
assert st == "absent", ("окно [from_bn+1 .. from_bn+N] покрыто целиком — приговор обязан "
                        "выноситься, иначе «мы правда теряем ноги» недоказуемо: %s" % (why,))
print("UNIT_OK: тихий блок стоит 1 запрос; окно приговора считается с from_bn+1 (блок отправки "
      "запечатан ДО неё), поэтому «absent» вообще достижим")

# ...а вот лёгкое чтение ВНУТРИ окна право на приговор отнимает. Сценарий боевой: прогрев
# гаснет через WANT_TTL, и хвост окна дочитывается легко.
calls5b = []
clk5b = Clock()
idx5b = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls5b),
                      now=clk5b, log_fn=lambda m: None)
idx5b.note_hash(h_ghost, head=BN + 5)
for i in range(6, 6 + ci.CHAIN_INDEX_ABSENT_BLOCKS + 1):
    if i == 9:
        clk5b.t += ci.CHAIN_INDEX_WANT_TTL_SEC + 1.0     # прогрев погас посреди окна
    idx5b.scan_block(BN + i)
st, why = idx5b.state(h_ghost)
assert st == "unknown" and str(BN + 9) in why, (st, why)
print("UNIT_OK: блок ВНУТРИ окна, прочитанный НЕ полностью, лишает права на приговор — "
      "«мы туда не смотрели» != «нашей tx там не было»")

# ---------------------------------------------------------------- 9. неделя штиля -> старт окна
clk6 = Clock()
calls6 = []
idx6 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls6),
                     now=clk6, log_fn=lambda m: None)
idx6.scan_block(BN - 1)
clk6.t += 7 * 24 * 3600.0                         # НЕДЕЛЯ ШТИЛЯ
assert idx6.fresh() is False and idx6.corpses() is None, \
    "протухший индекс обязан отдавать None, а не пустой список трупов"
st, why = idx6.state(h_known)
assert st == "unknown", (st, why)
idx6.note_hash(h_ghost, head=BN - 1)
idx6.scan_block(BN)                               # первое окно после штиля
assert idx6.fresh() is True, "после недели простоя первый же блок обязан ОЖИВИТЬ индекс"
assert len(idx6.block_facts(BN)["ours"]) == 33, "и он обязан снова ВИДЕТЬ наши пробы"
assert idx6.corpses() is not None
print("UNIT_OK: неделя штиля -> старт окна: индекс оживает с первого блока, а пока протух — "
      "молчит явно (None), а не пустотой")

# ---------------------------------------------------------------- 10. отказ узла — громкий
import logging                                                # noqa: E402

seen = []


class _Cap(logging.Handler):
    def emit(self, rec):
        seen.append(rec.getMessage())


_h = _Cap()
ci.log.addHandler(_h)
try:
    clk7 = Clock()
    idx7 = ci.ChainIndex("https://node.example/SECRETKEY", watch_to=[BOARD],
                         http=make_http([], fail={"eth_getLogs": "429 over rate limit"}),
                         now=clk7, log_fn=lambda m: None)
    try:
        idx7.scan_block(BN)
        raise AssertionError("отказ узла обязан лететь наружу, а не превращаться в пустой блок")
    except ci.IndexUnavailable:
        pass
    idx7._note_fail(RuntimeError("429"), BN)
finally:
    ci.log.removeHandler(_h)
assert seen and "ОТКАЗ" in seen[0], seen
assert "SECRETKEY" not in " ".join(seen), "ключ не имеет права попасть в журнал: %s" % seen
assert "node.example" in seen[0], "а ХОСТ обязан: без него отказ неадресуем"
print("UNIT_OK: отказ узла — громкая строка с хостом и БЕЗ ключа; молчаливой потери индекса нет")

# ---------------------------------------------------------------- 11. дрейф трупов по глубине
clk8 = Clock()
idx8 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http([]),
                     now=clk8, log_fn=lambda m: None)
idx8.scan_block(BN)
assert len(idx8.corpses()) == 1
idx8._ingest(BN + ci.CHAIN_INDEX_CORPSE_BLOCKS + 1, {}, full=True)
assert idx8.corpses() == {}, "труп старше окна обязан выпасть — иначе фильтр вечно режет живых"
print("UNIT_OK: список трупов ограничен окном в %d блоков" % ci.CHAIN_INDEX_CORPSE_BLOCKS)

# ---------------------------------------------------------------- 12. кольцо памяти
clk9 = Clock()
idx9 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http([]),
                     now=clk9, keep_blocks=4, log_fn=lambda m: None)
idx9.scan_block(BN)
assert len(idx9._by_hash) == 33
for i in range(1, 8):
    idx9._ingest(BN + i, {}, full=True)
assert idx9._by_hash == {} and idx9._blocks == {}, "кольцо обязано выселять старые блоки"
print("UNIT_OK: память ограничена кольцом (иначе 1175 tx × сутки = утечка на боевой машине)")

# ---------------------------------------------------------------- 13. откат чтения по чужим логам
_sv = ci.CHAIN_INDEX_FULL_ON_LOGS
try:
    ci.CHAIN_INDEX_FULL_ON_LOGS = False
    calls10 = []
    idx10 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls10),
                          now=Clock(), log_fn=lambda m: None)
    idx10.scan_block(BN)
    assert calls10 == ["eth_getLogs"], \
        "CHAIN_INDEX_FULL_ON_LOGS=0 обязан снимать полное чтение чужих гонок целиком: %s" % calls10
    assert idx10.corpses() and len(idx10.corpses()) == 1, \
        "но ТРУПЫ обязаны собираться и в этом режиме — они живут в логах, а не в блоке"
finally:
    ci.CHAIN_INDEX_FULL_ON_LOGS = _sv
print("UNIT_OK: ручка трафика снимает механизм полного чтения чужих гонок, не трогая трупы")

# ---------------------------------------------------------------- 13b. ПРИГОВОР ВЫНОСИТСЯ ПОЗЖЕ,
# ЧЕМ ГРЕЕТСЯ БЛОК. Класс дефекта «гейт проверяли в МОМЕНТЕ, а не во ВРЕМЕНИ»: право сказать
# «absent» нужно через 180с после отправки (таймаут коллектора), а режим полного чтения живёт
# 30с. Если забывать заявку вместе с прогревом, весь пакет обесценивается ровно в точке
# применения — тест проверяет ИМЕННО этот разрыв во времени.
import chain.fire_engine as _fe                                # noqa: E402

assert ci.CHAIN_INDEX_WANT_KEEP_SEC > _fe.COLLECT_TIMEOUT_SEC, \
    ("память индекса о хэше (%.0fс) обязана переживать таймаут коллектора (%.0fс)"
     % (ci.CHAIN_INDEX_WANT_KEEP_SEC, _fe.COLLECT_TIMEOUT_SEC))
assert ci.CHAIN_INDEX_KEEP_BLOCKS * 2 > _fe.COLLECT_TIMEOUT_SEC, \
    "кольцо блоков (%d × 2с) обязано переживать таймаут коллектора" % ci.CHAIN_INDEX_KEEP_BLOCKS

clk12 = Clock()
calls12 = []
idx12 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=make_http(calls12),
                      now=clk12, log_fn=lambda m: None)
idx12.note_hash(h_ghost, head=BN + 20)
idx12.scan_block(BN + 20)
assert idx12._want_full() is True, "сразу после отправки блоки обязаны читаться целиком"
for i in range(1, 30):                       # ~60с спустя: греть уже не надо…
    clk12.t += 2.0
    idx12.scan_block(BN + 20 + i)
assert idx12._want_full() is False, "…через TTL прогрев гаснет (трафик не жжём в пустую)"
clk12.t += 120.0                             # …а приговор выносится на 180-й секунде
idx12.scan_block(BN + 51)
st, why = idx12.state(h_ghost)
assert st == "absent", ("через 180с (таймаут коллектора) индекс ОБЯЗАН помнить заявку и "
                        "выносить приговор, а не отвечать «не регистрировался»: %s" % (why,))
print("UNIT_OK: прогрев гаснет за %.0fс, а ПАМЯТЬ о заявке живёт %.0fс — приговор на 180-й "
      "секунде выносится (гейт проверен ВО ВРЕМЕНИ, не в моменте)"
      % (ci.CHAIN_INDEX_WANT_TTL_SEC, ci.CHAIN_INDEX_WANT_KEEP_SEC))

# ---------------------------------------------------------------- 14. ОДИН долгоживущий тред
import threading                                              # noqa: E402
import time as _time                                          # noqa: E402

before = threading.active_count()
clk11 = Clock()
_h11 = {"n": BN + 3}
idx11 = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD],
                      http=make_http([], head=lambda: _h11["n"]),
                      now=clk11, log_fn=lambda m: None)
idx11.start()
try:
    peak = 0
    for bn in (BN, BN + 1, BN + 2, BN + 3):
        idx11.offer_block(bn)
        peak = max(peak, threading.active_count())
        _time.sleep(0.05)
    for _ in range(60):
        if idx11._last_bn == BN + 3:
            break
        _time.sleep(0.05)
    assert idx11._last_bn == BN + 3, "тред обязан догнать голову: %s" % idx11._last_bn
    assert peak - before <= 1, \
        ("тред-на-событие ЗАПРЕЩЁН (инцидент 15:41: Thread() на горячем пути пробил cgroup-кап "
         "и уронил hot_loop): было %d, пик %d" % (before, peak))
    assert idx11.alive()
finally:
    idx11.stop()
print("UNIT_OK: четыре блока подряд обслужены ОДНИМ долгоживущим тредом (+1 к процессу), "
      "подача блока в петлю сети не трогает")

print("ПРОШЛИ: фоновый индекс цепи (#110) — позитивный контроль на блоке %d" % BN)
