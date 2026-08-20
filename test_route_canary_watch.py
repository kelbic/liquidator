"""Канарейка выходов на кроне (#72): порог по LIF рынка, «НЕ ОПРОШЕН» не алертит.

ШОВ ТОЛЬКО НА ТРАНСПОРТЕ. Подменяется ровно `fetch(url, body) -> dict` (сокет узла).
Настоящими исполняются: сборка калдаты quoteExactInput, ретрай и классификация ошибок
узла (_call), арифметика impact (scan), карта адрес->символ, выбор порога из
covered_markets.json, отбор проблемных, отпечаток состояния и весь цикл run() с
повторными проходами. Канал тревоги — spy (это тоже транспорт).

КОНТРОЛИ (доказывают, что тест умеет падать) — блок «САМОКОНТРОЛЬ» внизу: те же самые
проверки прогоняются на НАМЕРЕННО сломанных вариантах и обязаны провалиться.
"""
import json
import sys
import urllib.error

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from analysis import route_canary as rc
from analysis import route_canary_watch as w

FAILED = []


def check(cond, msg):
    if not cond:
        FAILED.append(msg)
        print("  FAIL: " + msg)
    return bool(cond)


# ── стенд: узел, у которого пулы ведут себя так, как мы велим ──────────────────────
REAL_SEL = rc.SEL          # снят ДО подмен: контроль (в) подменяет rc.SEL


class Node:
    """Фальшивый узел. Разбирает НАСТОЯЩУЮ калдату (селектор, offset, amount, path),
    поэтому ошибка в упаковке пути поймается здесь, а не в бою."""

    def __init__(self, impacts, px=10 ** 6, revert=(), limit=(), limit_passes=0,
                 limit_kind="http"):
        self.impacts = impacts          # token_in(lower) -> impact на рабочем объёме, доля
        self.px = px
        self.revert = set(revert)       # token_in -> execution reverted
        self.limit = set(limit)         # token_in -> узел отшивает по ставке (429)
        self.limit_passes = limit_passes  # сколько ПРОХОДОВ отшивать (потом отвечает)
        self.limit_kind = limit_kind    # http: HTTPError 429 | json: JSON-RPC «rate limit»
        self.calls = 0
        self.bad_sel = 0                # калдата пришла не с тем селектором
        self.pass_no = 1
        self.work = {}

    def __call__(self, url, body):
        self.calls += 1
        req = json.loads(body)
        data = req["params"][0]["data"]
        if data[2:10] != REAL_SEL:
            self.bad_sel += 1
        amount = int(data[74:138], 16)
        plen = int(data[138:202], 16)
        path = data[202:202 + plen * 2]
        tin = "0x" + path[:40]
        if tin in self.limit and self.pass_no <= self.limit_passes:
            if self.limit_kind == "http":
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
            return {"jsonrpc": "2.0", "id": 1,          # так режут dRPC/Alchemy: не сокетом
                    "error": {"code": -32005, "message": "rate limit exceeded"}}
        if tin in self.revert:
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": 3, "message": "execution reverted"}}
        imp = self.impacts.get(tin, 0.0)
        work = self.work.get(tin, amount)
        price = self.px if amount <= work // 50 else int(self.px * (1 - imp))
        return {"jsonrpc": "2.0", "id": 1, "result": "0x%064x" % (amount * price // 10 ** 6)}


class Spy:
    def __init__(self, ret="inbox"):
        self.calls = []
        self.ret = ret

    def __call__(self, text, *, source, hil=False, key="", dedup_sec=0.0):
        self.calls.append({"text": text, "source": source, "hil": hil,
                           "key": key, "dedup_sec": dedup_sec})
        return self.ret


MARKETS = [
    {"collateral_token": "cbDOGE", "loan_token": "USDC", "lltv": 0.625, "bonus": 0.126761},
    {"collateral_token": "cbBTC", "loan_token": "USDC", "lltv": 0.86, "bonus": 0.043841},
    {"collateral_token": "wstETH", "loan_token": "WETH", "lltv": 0.945, "bonus": 0.016777},
    # вторая запись той же пары — тесть выбора МИНИМАЛЬНОГО бонуса
    {"collateral_token": "wstETH", "loan_token": "WETH", "lltv": 0.86, "bonus": 0.043841},
]


def routes_subset(names):
    """Подмножество БОЕВОЙ карты маршрутов + опциональная подмена капа (копия, не мутация)."""
    items, module = rc.routes()
    by = {}
    for (c, l), r in items:
        by[(c, l)] = r
    out = []
    for key, cap in names:
        r = dict(by[key])
        if cap is not None:
            r["max_in"] = cap
        out.append((key, r))
    return out, module


def run_case(items, module, node, **kw):
    node.work = {c.lower(): (r.get("max_in") or 10 ** 8) for (c, _l), r in items}
    spy = Spy()
    lines = []
    slept = []
    s = w.run(items=items, module=module, notify_fn=spy, markets=MARKETS,
              out=lines.append, sleep=slept.append, fetch=node, pause=0, tries=2, **kw)
    s["spy"], s["lines"], s["slept"], s["node"] = spy, lines, slept, node
    return s


_, _MOD = rc.routes()
_CBDOGE = _MOD._CBDOGE
_CBBTC = _MOD._CBBTC
_USDC = _MOD._UNIV3_USDC
_CBLTC = _MOD._CBLTC
_WSTETH = _MOD._WSTETH
_WETH = _MOD._UNIV3_WETH

DOGE = ((_CBDOGE, _USDC), None)
BTC = ((_CBBTC, _USDC), None)
WSTETH = ((_WSTETH, _WETH), None)

print("=== 1. Порог берётся из LIF конкретного рынка, а не из общих 10% ===")
th, src = w.threshold_for("cbDOGE", "USDC", MARKETS)
check(abs(th - 12.6761) < 1e-6, "cbDOGE/USDC порог = бонус 12.68%%, получено %s" % th)
check("LIF cbDOGE/USDC" in src, "источник называет рынок: %s" % src)
th_btc, _ = w.threshold_for("cbBTC", "USDC", MARKETS)
check(abs(th_btc - 4.3841) < 1e-6, "cbBTC/USDC порог = бонус 4.38%%, получено %s" % th_btc)
th_multi, src_multi = w.threshold_for("wstETH", "WETH", MARKETS)
check(abs(th_multi - 1.6777) < 1e-6,
      "пара с двумя рынками берёт МИНИМАЛЬНЫЙ бонус, получено %s" % th_multi)
check("рынков 2" in src_multi, "источник называет число рынков: %s" % src_multi)
th_def, src_def = w.threshold_for("WETH", "USDC", MARKETS)
check(th_def == 10.0 and "дефолт" in src_def, "пары нет в файле -> дефолт 10%%: %s" % src_def)
print("UNIT_OK: порог = LIF рынка (мин. бонус пары), иначе дефолт 10%")

print("\n=== 1б. #76: пара, которой нет в covered_markets, судится по lltv С ЦЕПИ ===")
# WETH/USDC (0x8793cf30) живёт в INCLUDE_MARKETS, а в covered_markets.json его нет — на
# БОЕВОЙ карте роутов канарейка выдавала ему дефолт 10% при настоящем бонусе 4.38%, то есть
# молчала бы при выходе, съедающем бонус ВДВОЕ. Порог, врущий про запас, опаснее его
# отсутствия. Проверяем на боевых адресах, а не на синтетике.
_real_rows = [{"coll": c, "loan": l} for (c, l), _r in rc.routes()[0]]
_real_th = w.thresholds_for_rows(_real_rows, markets=w.load_markets())
_wu = _real_th[(_WETH, _USDC)]
check(abs(_wu[0] - 4.3841) < 1e-3, "WETH/USDC порог = 4.38%% с цепи, получено %.4f%%" % _wu[0])
check("дефолт" not in _wu[1], "источник больше не дефолт: %s" % _wu[1])
# и обратная сторона: НИ ОДИН боевой роут не остаётся на щедром дефолте
_defaults = [k for k, (_p, s) in _real_th.items() if "дефолт" in s]
check(not _defaults, "роутов на дефолтном пороге быть не должно: %s" % _defaults)
# cbXRP: на паре два рынка, порог обязан быть по ТЕСНОМУ (7.41%), а не по жирному (12.68%)
_xr = _real_th[(_MOD._CBXRP, _USDC)]
check(abs(_xr[0] - 7.4114) < 1e-3, "cbXRP/USDC порог = 7.41%%, получено %.4f%%" % _xr[0])
print("UNIT_OK: пары вне covered_markets судятся по lltv с цепи (WETH/USDC 4.38%, не 10%)")

print("\n=== 2. Карта адрес->символ строится из констант chain.execute ===")
syms = w.token_symbols(module=_MOD, markets=MARKETS)
check(syms.get(_CBDOGE) == "cbDOGE", "cbDOGE опознан: %s" % syms.get(_CBDOGE))
check(syms.get(_USDC) == "USDC", "_UNIV3_USDC -> USDC: %s" % syms.get(_USDC))
check(syms.get(_WETH) == "WETH", "_UNIV3_WETH -> WETH: %s" % syms.get(_WETH))
check(w.norm_name("_UNIV3_USDC") == "usdc", "нормализация имени венью")
print("UNIT_OK: символы из констант, префикс венью снят")

print("\n=== 3. Здоровый маршрут не алертит; протухший кап — алертит ===")
items, module = routes_subset([DOGE, BTC])
ok = run_case(items, module, Node({_CBDOGE: 0.0094, _CBBTC: 0.0168}))
check(ok["problems"] == 0 and ok["alert"] == "нет",
      "здоровые роуты: проблем=%d alert=%s" % (ok["problems"], ok["alert"]))
check(len(ok["spy"].calls) == 0, "здоровые роуты: канал молчит")
check(ok["node"].bad_sel == 0, "калдата собрана настоящим селектором quoteExactInput")
# 5 = 2 маршрута x 2 объёма + ОДИН eth_blockNumber на проход (пин блока, 19.08). Заглушка
# узла номер блока не отдаёт (её __call__ разбирает только eth_call) — канарейка обязана
# продолжить на «latest», а не ослепнуть: неумение узла назвать блок не факт о маршруте.
check(ok["node"].calls == 5, "два маршрута опрошены на двух объёмах + пин: %d" % ok["node"].calls)
check(all(r["block"] == "latest" for r in ok["rows"]),
      "узел без eth_blockNumber -> замер на latest, а не отказ")

stale, module = routes_subset([((_CBDOGE, _USDC), 100_000 * 10 ** 8), BTC])
bad = run_case(stale, module, Node({_CBDOGE: 0.4350, _CBBTC: 0.0168}))
check(bad["problems"] == 1, "протухший кап даёт ровно одну проблему: %d" % bad["problems"])
check(len(bad["spy"].calls) == 1, "алерт сформирован ровно один")
if bad["spy"].calls:
    a = bad["spy"].calls[0]
    check("cbDOGE→USDC" in a["text"], "алерт называет пару: %s" % a["text"][:120])
    check("43.5" in a["text"] and "12.68" in a["text"],
          "алерт несёт impact и порог: %s" % a["text"][:200])
    check("LIF cbDOGE/USDC" in a["text"], "алерт называет ИСТОЧНИК порога")
    check("max_in=10000000000000" in a["text"].replace(" ", ""),
          "алерт называет кап (действие = кап вниз): %s" % a["text"][:200])
    check(a["hil"] is False, "адресат — агент, не человек (hil=False)")
    check(a["source"] == "route-canary", "подпись источника")
    check(a["dedup_sec"] >= 3600, "у тревоги есть ре-арм, а не звонок каждые 6ч")
print("UNIT_OK: тревога формируется на протухшем капе и молчит на здоровом")

print("\n=== 4. Порог ПЕРСОНАЛЬНЫЙ: 6% на cbBTC — тревога, 11% на cbDOGE — нет ===")
mixed, module = routes_subset([DOGE, BTC])
m = run_case(mixed, module, Node({_CBDOGE: 0.11, _CBBTC: 0.06}))
SYMS = w.token_symbols(module=module, markets=MARKETS)
names = [p["name"] for p in w.evaluate(m["rows"], m["thresholds"], SYMS)]
check(names == ["cbBTC→USDC"],
      "тревожит cbBTC (6%%>4.38%%), молчит cbDOGE (11%%<12.68%%): %s" % names)
check(m["problems"] == 1, "ровно одна проблема, а не две и не ноль: %d" % m["problems"])
print("UNIT_OK: общий порог 10%% дал бы ровно обратный ответ на обоих роутах")

print("\n=== 5. «НЕ ОПРОШЕН» не алертит, но вызывает ПОВТОРНЫЙ проход ===")
items, module = routes_subset([DOGE, BTC])
node = Node({_CBDOGE: 0.0094, _CBBTC: 0.0168}, limit=[_CBDOGE], limit_passes=1)
slept = []
spy = Spy()
node.work = {c.lower(): (r.get("max_in") or 10 ** 8) for (c, _l), r in items}


def _sleep(sec):
    slept.append(sec)
    node.pass_no += 1               # узел «отпускает» лимит со следующего прохода


lines = []
s = w.run(items=items, module=module, notify_fn=spy, markets=MARKETS, out=lines.append,
          sleep=_sleep, fetch=node, pause=0, tries=2)
check(s["passes"] == 2, "неопрошенный маршрут вызвал второй проход: passes=%d" % s["passes"])
check(len(slept) == 1 and slept[0] > 0, "между проходами есть пауза: %s" % slept)
check(s["unpolled"] == 0, "после повторного прохода неопрошенных нет: %d" % s["unpolled"])
check(s["dead"] == 0, "лимит узла НЕ засчитан в МЁРТВ: dead=%d" % s["dead"])
check(len(spy.calls) == 0, "по лимиту узла тревоги нет: %s" % [c["text"][:60] for c in spy.calls])

# и если узел не отпустил лимит совсем — по-прежнему НИ ОДНОЙ тревоги (но статус виден)
node2 = Node({_CBDOGE: 0.0094, _CBBTC: 0.0168}, limit=[_CBDOGE], limit_passes=99)
s2 = run_case(items, module, node2)
check(s2["unpolled"] == 1 and s2["dead"] == 0,
      "устойчивый лимит = НЕ ОПРОШЕН, не МЁРТВ: unpolled=%d dead=%d"
      % (s2["unpolled"], s2["dead"]))
check(len(s2["spy"].calls) == 0, "устойчивый лимит узла всё равно не алертит")
check(s2["passes"] == 3, "исчерпаны все проходы: %d" % s2["passes"])

# лимит приезжает и не сокетом: провайдеры режут ставку JSON-RPC-ошибкой
s3 = run_case(items, module, Node({_CBDOGE: 0.0094, _CBBTC: 0.0168}, limit=[_CBDOGE],
                                  limit_passes=99, limit_kind="json"))
check(s3["unpolled"] == 1 and s3["dead"] == 0,
      "JSON-RPC «rate limit exceeded» = НЕ ОПРОШЕН: unpolled=%d dead=%d"
      % (s3["unpolled"], s3["dead"]))
check(len(s3["spy"].calls) == 0, "по JSON-RPC-лимиту тревоги нет")
print("UNIT_OK: лимит узла (429 и JSON-RPC) = повторный проход и тишина в канале")

print("\n=== 6. Реверт квотера — это МЁРТВ и тревога (положительное свидетельство) ===")
d = run_case(items, module, Node({_CBBTC: 0.0168}, revert=[_CBDOGE]))
check(d["dead"] == 1, "реверт = МЁРТВ: %d" % d["dead"])
check(len(d["spy"].calls) == 1, "мёртвый маршрут алертит")
check("МЁРТВ" in d["spy"].calls[0]["text"] and "execution reverted" in d["spy"].calls[0]["text"],
      "алерт несёт причину узла: %s" % d["spy"].calls[0]["text"][:160])
print("UNIT_OK: реверт отличён от лимита узла и разбужен алертом")

print("\n=== 7. Полная слепота — отказ СТОРОЖА, он обязан быть слышен ===")
blind = run_case(items, module, Node({}, limit=[_CBDOGE, _CBBTC], limit_passes=99))
check(blind["blind"] is True, "все маршруты не опрошены => слепота")
check(len(blind["spy"].calls) == 1, "слепота алертит ровно один раз")
check("ОСЛЕПЛА" in blind["spy"].calls[0]["text"], "текст называет отказ сторожа, а не роутов")
check(blind["spy"].calls[0]["hil"] is False, "слепота — тоже к агенту")
print("UNIT_OK: слепота названа своим именем (мёртвый сторож хуже отсутствующего)")

print("\n=== 8. Отпечаток СОСТОЯНИЯ: тот же набор = тот же ключ, новый роут = новый ===")
p1 = [{"name": "a→b", "kind": "deep"}]
p2 = [{"name": "a→b", "kind": "deep"}, {"name": "c→d", "kind": "dead"}]
check(w.fingerprint(p1) == w.fingerprint(list(p1)), "отпечаток устойчив")
check(w.fingerprint(p1) != w.fingerprint(p2), "новый проблемный роут меняет отпечаток")
check(w.fingerprint(p1) != w.fingerprint([{"name": "a→b", "kind": "dead"}]),
      "смена рода проблемы меняет отпечаток")
check(w.fingerprint([], blind=True) != w.fingerprint([]), "слепота — отдельный субъект")
print("UNIT_OK: ключ дедупа = отпечаток состояния, без числового шума")

print("\n=== 9. Лог прогона: одна строка, все счётчики, метка времени ===")
tail = [l for l in bad["lines"] if "route-canary:" in l]
check(len(tail) == 1, "ровно одна сводная строка: %d" % len(tail))
if tail:
    for frag in ("маршрутов=", "жив=", "мёртв=", "не_опрошен=", "проблем=", "alert=", "fp="):
        check(frag in tail[0], "в строке лога есть %s: %s" % (frag, tail[0]))
    check(tail[0].startswith("20") and "T" in tail[0].split()[0], "строка начинается с UTC-метки")
print("UNIT_OK: %s" % (tail[0] if tail else "—"))

print("\n=== 10. Канал тревоги в тестовом прогоне физически закрыт ===")
try:
    sys.path.insert(0, w.WATCH_DIR)
    import notify as fleet_notify
    check(fleet_notify.muted() is True,
          "боевой notify обязан быть замьючен под тестом (сторож на транспорте)")
    print("UNIT_OK: notify.muted() == True — боевая очередь тревог не пачкается")
except ImportError:
    print("SKIP: ~/.fleet-watch/notify.py недоступен в этой среде")

# ── САМОКОНТРОЛЬ: те же проверки на сломанных вариантах ОБЯЗАНЫ провалиться ────────
# --- 19.08: ПИН БЛОКА. impact — отношение ДВУХ котировок, и на «latest» они уезжают в разные
# блоки: чужой своп между ними приезжает в impact как «форма пула», а по этому числу режут капы.
print("\n=== 11. Обе котировки маршрута берутся на ОДНОМ блоке (пин) ===")


class BlockNode(Node):
    """Узел, который умеет eth_blockNumber. no_state=блок, на котором состояния нет (не архив)."""

    def __init__(self, *a, blk="0x2fdef60", no_state=False, **kw):
        super().__init__(*a, **kw)
        self.blk = blk
        self.no_state = no_state
        self.seen_blocks = []

    def __call__(self, url, body):
        req = json.loads(body)
        if req.get("method") == "eth_blockNumber":
            self.calls += 1
            return {"jsonrpc": "2.0", "id": 1, "result": self.blk}
        self.seen_blocks.append(req["params"][1])
        if self.no_state and req["params"][1] != "latest":
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32000, "message": "missing trie node"}}
        return super().__call__(url, body)


items, module = routes_subset([DOGE, BTC])
pinned = run_case(items, module, BlockNode({_CBDOGE: 0.0094, _CBBTC: 0.0168}))
check(all(b == "0x2fdef60" for b in pinned["node"].seen_blocks),
      "все котировки ушли на пине: %s" % sorted(set(pinned["node"].seen_blocks)))
check(len(pinned["node"].seen_blocks) == 4 and pinned["problems"] == 0,
      "пин не добавил лишних квот и не создал проблем: %d квот, проблем %d"
      % (len(pinned["node"].seen_blocks), pinned["problems"]))
check(all(r["block"] == "0x2fdef60" for r in pinned["rows"]), "блок замера записан в строку")

# Узел БЕЗ архива обязан дать тот же вердикт, что и раньше: пин — оптимизация точности,
# а не новое условие живости. Иначе канарейка объявила бы МЁРТВЫМ живой роут на не-архивном
# узле — ровно тот класс, из-за которого «НЕ ОПРОШЕН» когда-то отделили от «МЁРТВ».
noarch = run_case(items, module, BlockNode({_CBDOGE: 0.0094, _CBBTC: 0.0168}, no_state=True))
check([r["status"] for r in noarch["rows"]] == [rc.ALIVE, rc.ALIVE],
      "узел без состояния на пине -> перезапрос на latest, роуты живы: %s"
      % [r["status"] for r in noarch["rows"]])
check(noarch["problems"] == 0 and noarch["alert"] == "нет",
      "и никакой ложной тревоги: проблем=%d alert=%s" % (noarch["problems"], noarch["alert"]))

# --- 20.08: НОМЕР БЛОКА В ТЕКСТЕ ТРЕВОГИ. Разбор тревоги начинается с реквоты на том же
# блоке; без номера разбирающий восстанавливает его руками по времени (так и вышло 19.08).
print("\n=== 12. Тревога называет блок замера (воспроизводимость) ===")

items12, module12 = routes_subset([((_CBDOGE, _USDC), 100_000 * 10 ** 8), BTC])
pin = run_case(items12, module12, BlockNode({_CBDOGE: 0.4350, _CBBTC: 0.0168},
                                            blk="0x2fdef60"))
alert12 = "\n".join(p["text"] for p in w.evaluate(pin["rows"], pin["thresholds"],
                                                  symbols=w.token_symbols(module=module12,
                                                                          markets=MARKETS)))
check(pin["problems"] >= 1, "стенд обязан дать хотя бы одну проблему: %d" % pin["problems"])
check("блок замера 50196320" in alert12,
      "номер блока (десятичный) есть в тексте тревоги: %r" % alert12[:160])

# Замер на «latest» НЕ обещает воспроизводимости — и обязан молчать о блоке, а не врать.
noblk = run_case(items12, module12, Node({_CBDOGE: 0.4350, _CBBTC: 0.0168}))
alert_nb = "\n".join(p["text"] for p in w.evaluate(noblk["rows"], noblk["thresholds"],
                                                   symbols=w.token_symbols(module=module12,
                                                                           markets=MARKETS)))
check(noblk["problems"] >= 1, "стенд latest обязан дать проблему: %d" % noblk["problems"])
check("блок замера" not in alert_nb,
      "на latest про блок НЕ обещается ничего: %r" % alert_nb[:160])
check(w.at_block({"block": "мусор"}) == "" and w.at_block({}) == "" and w.at_block(None) == "",
      "мусор/отсутствие блока -> пусто, а не исключение")

# --- 20.08: СПРАВКА О БОЕВОМ КАПЕ. Разбор CBLTC 20.08: канарейка кричала «impact 29.03%
# при бонусе 12.68%», а бот в ту же минуту держал роут на 105 cbLTC вместо 280 (live-cap
# уже срезал). Тревога была права про ПУЛ и молчала про то, покрыт ли он; выяснение стоило
# захода в чужой журнал. Теперь вердикт боевой политики едет в тексте.
print("\n=== 13. Тревога говорит, режет ли боевой live-cap этот размер ===")

_probs13 = [{"kind": "deep", "name": "CBLTC→USDC", "text": "CBLTC→USDC AERO impact 29.03%",
             "row": {"coll": _CBLTC, "loan": _USDC}, "th": 12.68, "src": "LIF"}]

# (а) политика режет кап втрое -> тревога обязана это СКАЗАТЬ
w.annotate_policy(_probs13, measure=lambda *a, **k: 10_500_000_000)
check("политика капа на сегодня: 10500000000" in _probs13[0]["text"],
      "вердикт политики есть в тексте: %r" % _probs13[0]["text"])
check("живой слой" not in _probs13[0]["text"] or "режет размер сам" in _probs13[0]["text"],
      "срез назван срезом: %r" % _probs13[0]["text"])

# (б) политика НЕ режет -> сказано, что судимый размер и есть боевой
_probs13b = [dict(_probs13[0], text="CBLTC→USDC AERO impact 29.03%")]
w.annotate_policy(_probs13b, measure=lambda *a, **k: 28_000_000_000)
check("НЕ режет" in _probs13b[0]["text"],
      "равный кап назван равным: %r" % _probs13b[0]["text"])

# (в) ЛИМИТ УЗЛА НЕ ИМЕЕТ ПРАВА СТАТЬ ЧИСЛОМ. Ступень, которую узел не обслужил, в бою
# читается как «пул ревертит -> обрыв -> режем». Здесь это было бы фабрикацией: мы не
# мерили. Незнание обязано закрывать гард — справки просто нет.
_probs13c = [dict(_probs13[0], text="CBLTC→USDC AERO impact 29.03%")]
_limit_call = lambda to, data, **kw: (None, "429 (НЕ ОПРОШЕН)", rc.UNPOLLED)
w.annotate_policy(_probs13c, call=_limit_call)
check("НЕ ПОСЧИТАНА" in _probs13c[0]["text"],
      "лимит узла -> «не посчитана», а не выдуманный кап: %r" % _probs13c[0]["text"])
for _bad in ("политика капа на сегодня", "НЕ режет"):
    check(_bad not in _probs13c[0]["text"],
          "при лимите узла НЕ печатается вердикт %r: %r" % (_bad, _probs13c[0]["text"]))

# (г) СПРАВКА НЕ УПРАВЛЯЕТ ТРЕВОГОЙ. Что бы ни случилось с замером политики, состав
# проблем и решение «звонить» обязаны остаться прежними.
_probs13d = [dict(_probs13[0], text="X"), {"kind": "dead", "name": "Y", "text": "Y",
                                            "row": {"coll": _CBLTC, "loan": _USDC}}]
_before = [(x["kind"], x["name"]) for x in _probs13d]
_fp_before = w.fingerprint(_probs13d)
w.annotate_policy(_probs13d, measure=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("бум")))
check([(x["kind"], x["name"]) for x in _probs13d] == _before,
      "падение замера политики не меняет состав проблем")
check(w.fingerprint(_probs13d) == _fp_before,
      "справка не сдвигает отпечаток состояния (ключ дедупа)")
check("МЁРТВ" not in _probs13d[1]["text"] and _probs13d[1]["text"] == "Y",
      "к мёртвому маршруту справка о капе не дописывается")

print("UNIT_OK: справка о боевом капе есть, лимит узла числом не притворяется, "
      "тревога от справки не зависит")

print("\n=== САМОКОНТРОЛЬ (тест обязан уметь падать) ===")
ctrl = []

# (а) если классификатор лимита узла сломать — 429 поедет в МЁРТВ и даст ложную тревогу
_real = rc.is_node_limit
rc.is_node_limit = lambda m: False
try:
    broken = run_case(items, module, Node({_CBDOGE: 0.0094, _CBBTC: 0.0168},
                                          limit=[_CBDOGE], limit_passes=99,
                                          limit_kind="json"))
    ctrl.append(("лимит узла без классификатора => ложная тревога",
                 broken["dead"] == 1 and len(broken["spy"].calls) == 1))
finally:
    rc.is_node_limit = _real

# (б) если порог взять общий 10% вместо LIF — ответ на обоих роутах переворачивается
flat = w.evaluate(m["rows"], {k: (10.0, "плоский") for k in m["thresholds"]}, SYMS)
ctrl.append(("плоские 10%% дают ровно обратный набор",
             [p["name"] for p in flat] == ["cbDOGE→USDC"]))

# (в) подменённый селектор обязан быть ЗАМЕЧЕН стендом — иначе проверка калдаты слепа
_qsel = rc.SEL
rc.SEL = "deadbeef"
try:
    probe = run_case(items, module, Node({_CBDOGE: 0.0094, _CBBTC: 0.0168}))
finally:
    rc.SEL = _qsel
ctrl.append(("подмена селектора ловится проверкой калдаты", probe["node"].bad_sel > 0))

for name, ok_ in ctrl:
    print(("  контроль ok:   " if ok_ else "  КОНТРОЛЬ СЛЕП: ") + name)
    if not ok_:
        FAILED.append("контроль не сработал: " + name)

print()
if FAILED:
    print("ПРОВАЛЫ (%d):" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("OK: ПРОШЛИ все проверки канарейки выходов (#72)")
