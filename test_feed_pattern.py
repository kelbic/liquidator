"""Юнит-тест watch-паттернов (WATCH_PATTERN_OVERRIDES): read-time композит-оракул подменяется
hex-подстрокой update-tx, чтобы bare-match ловил апдейты стороннего push-адаптера (LBTC/cbBTC:
RedStone LBTC_FUNDAMENTAL). Проверяет: (A) _resolve_one подставляет паттерн вместо композита и
не трогает обычные агрегаторы, (B) bare-match условие hotpath находит паттерн в raw и НЕ матчит
чужие апдейты того же адаптера, (C) match_aggs симметричен, (D) паттерн-ключ отличим от адреса
(len != 42) и в нижнем регистре, (E) resolve_feeds группирует рынок под ключом-паттерном.
Без сети: rpc замокан."""
import sys
sys.path.insert(0, ".")
import chain.feeds as feeds

COMPOSITE = "0x31d099c106cd73e731972fdf1390cab77f59dade"
PATTERN = "0x" + "LBTC_FUNDAMENTAL".encode().hex()
MID = "0x4944a1169bc07b441473b830308ffe5bb535c10a9f824e33988b60738120c48e"
PLAIN_AGG = "0x" + "ab" * 20


class _Mkt:
    def __init__(self, mid):
        self.market_id = mid


def _norm_legs(v):
    """Значение mapping: скаляр (одноногий рынок, primary=нога), кортеж (primary, legs) для
    multi-leg, None (вне карты) — в форму _resolve_legs."""
    if v is None:
        return None, []
    if isinstance(v, tuple):
        return v
    return v, [v]


def _patched_resolve(mapping):
    """resolve_feeds поверх замоканного _resolve_legs (I/O-шов из docstring)."""
    orig = feeds._resolve_legs
    feeds._resolve_legs = lambda rpc, mid, fb=None: _norm_legs(mapping.get(mid))
    try:
        return feeds.resolve_feeds(None, [_Mkt(m) for m in mapping])
    finally:
        feeds._resolve_legs = orig


REDSTONE_FEED = "0x5c4c8d6f6bf79b718f3e8399aabdfed01cb7e48f"
MID2 = "0x30767836635facec1282e6ef4a5981406ed4e72727b3a63a3a72c74e8279a8d7"


def test_override_substitutes_pattern():
    # (A) прямой вызов таблицы: композит и прямой RedStone-фид -> ОДИН паттерн, обычный адрес -> сам себя
    assert feeds.WATCH_PATTERN_OVERRIDES[COMPOSITE] == PATTERN
    assert feeds.WATCH_PATTERN_OVERRIDES[REDSTONE_FEED] == PATTERN
    assert feeds.WATCH_PATTERN_OVERRIDES.get(PLAIN_AGG, PLAIN_AGG) == PLAIN_AGG


def test_two_lbtc_markets_group_under_one_pattern():
    # (A2) оба LBTC-рынка (0.915 через композит, 0.945 напрямую через фид) под одним ключом ->
    # один триггер ассессит оба (feeds.get(pattern) в _process_transmit)
    out = _patched_resolve({MID: PATTERN, MID2: PATTERN})
    assert sorted(out[PATTERN]) == sorted([MID, MID2])
    assert len(out) == 1


def test_pattern_key_shape():
    # (D) bare-match и DIAG-ветка hotpath различают паттерн по длине; hex-подстрока в lower
    assert len(PATTERN) != 42
    assert PATTERN == PATTERN.lower()
    assert PATTERN[2:] == "4c4254435f46554e44414d454e54414c"


def test_bare_match_condition():
    # (B) ровно то условие, что в hotpath: agg[2:] in raw (raw уже lower из extract_txs)
    raw_lbtc = "f9012a94b81131b6" + "00" * 8 + PATTERN[2:] + "beef"
    raw_other = "f9012a94b81131b6" + ("KAITO".encode().hex() + "00" * 27)
    assert PATTERN[2:] in raw_lbtc
    assert PATTERN[2:] not in raw_other


def test_match_aggs_with_pattern():
    # (C) match_aggs: матч по raw, отсутствие ложного матча по to=адаптер
    txs = [("aa" + PATTERN[2:] + "bb", "0xb81131b6368b3f0a83af09db4e39ac23da96c2db"),
           ("deadbeef", "0xb81131b6368b3f0a83af09db4e39ac23da96c2db")]
    assert feeds.match_aggs(txs, {PATTERN}) == {PATTERN}
    assert feeds.match_aggs([txs[1]], {PATTERN}) == set()


def test_resolve_feeds_groups_under_pattern():
    # (E) карта feeds: рынок LBTC живёт под ключом-паттерном, соседний — под своим агрегатором
    out = _patched_resolve({MID: PATTERN, "0x" + "cd" * 32: PLAIN_AGG})
    assert out[PATTERN] == [MID]
    assert out[PLAIN_AGG] == ["0x" + "cd" * 32]


def test_resolve_feeds_fallback_rpc():
    # (F) шторм на основном RPC (все пусто) -> пер-рыночный повтор через fallback_rpc;
    # легитимно пустой (не-Chainlink) остаётся пустым и на фолбэке
    MAIN, FB = object(), object()
    table = {(FB, MID): PATTERN, (FB, "0x" + "cd" * 32): PLAIN_AGG}   # main отдаёт пусто по всем
    orig = feeds._resolve_legs
    feeds._resolve_legs = lambda rpc, mid, fb=None: _norm_legs(table.get((rpc, mid)))
    try:
        blind = feeds.resolve_feeds(MAIN, [_Mkt(MID), _Mkt("0x" + "cd" * 32), _Mkt("0x" + "ee" * 32)])
        assert blind == {}                                            # без фолбэка — слепота (инцидент 07-23)
        out = feeds.resolve_feeds(MAIN, [_Mkt(MID), _Mkt("0x" + "cd" * 32), _Mkt("0x" + "ee" * 32)],
                                  fallback_rpc=FB)
        assert out[PATTERN] == [MID] and out[PLAIN_AGG] == ["0x" + "cd" * 32]
        assert len(out) == 2                                          # 0xee-рынок легитимно вне карты
    finally:
        feeds._resolve_legs = orig


def test_init_resolve_retry_empty_ok():
    # (G) empty_ok=False: {} = отказ -> ретраи; после исчерпания -> {} (слепой старт, не смерть треда);
    # empty_ok=True (meta-путь): {} возвращается сразу, поведение не изменилось
    import chain.hotpath as hp
    calls = {"n": 0}
    def _empty():
        calls["n"] += 1
        return {}
    out = hp._init_resolve_retry(_empty, what="t", tries=3, base=0.0, cap=0.0, empty_ok=False)
    assert out == {} and calls["n"] == 3
    calls["n"] = 0
    out = hp._init_resolve_retry(_empty, what="t", tries=3, base=0.0, cap=0.0, empty_ok=True)
    assert out == {} and calls["n"] == 1
    seq = iter([{}, {"0xa": ["m"]}])
    out = hp._init_resolve_retry(lambda: next(seq), what="t", tries=3, base=0.0, cap=0.0, empty_ok=False)
    assert out == {"0xa": ["m"]}


class HTTPError(Exception):
    """Имя типа = транспорт-класс из feeds._TRANSPORT_ERRS (классификация по имени)."""


class _FakeRpc:
    """rpc.contract(addr, abi).functions.<name>(*args).call() по таблице {(addr_lower, fn): value};
    отсутствие ключа = Exception (как живой revert/нет функции) либо HTTPError при
    transport_fail=True (как 429/сеть). args игнорируются (один рынок)."""
    def __init__(self, table, transport_fail=False):
        self._t = table
        self._transport = transport_fail

    def to_bytes32(self, x):
        return x

    def contract(self, addr, abi):
        table, key, transport = self._t, addr.lower(), self._transport

        class _Fns:
            def __getattr__(self, name):
                def _fn(*a):
                    class _Call:
                        def call(_self, **kw):
                            if (key, name) not in table:
                                if transport:
                                    raise HTTPError("429 Too Many Requests")
                                raise Exception("no %s on %s" % (name, key))
                            return table[(key, name)]
                    return _Call()
                return _fn

        class _Ct:
            functions = _Fns()
        return _Ct()


ORACLE2 = "0x" + "0a" * 20
BF1_PROXY, BF1_AGG = "0x" + "b1" * 20, "0x" + "a1" * 20
BF2_PROXY, BF2_AGG = "0x" + "b2" * 20, "0x" + "a2" * 20
QF1_PROXY, QF1_AGG = "0x" + "c1" * 20, "0x" + "d1" * 20
QF2_PROXY = "0x" + "c2" * 20


def _morpho_key():
    from chain.morpho import MORPHO_BLUE_ADDRESS
    return MORPHO_BLUE_ADDRESS.lower()


def test_resolve_legs_multileg_age_filter():
    # (H) BF1 = primary БЕЗ возраст-фильтра (стейбл-BF1 не выпадает из watch — статус-кво);
    # BF2 стейбл (age 25ч) -> отсев; QF1 живая (age 60с) -> в watch; QF2 hardcoded
    # (нет aggregator/latestTimestamp) -> отсев
    import time as _t
    now = _t.time()
    rpc = _FakeRpc({
        (_morpho_key(), "idToMarketParams"): ("L", "C", ORACLE2, "I", 0),
        (ORACLE2, "BASE_FEED_1"): BF1_PROXY,
        (ORACLE2, "BASE_FEED_2"): BF2_PROXY,
        (ORACLE2, "QUOTE_FEED_1"): QF1_PROXY,
        (ORACLE2, "QUOTE_FEED_2"): QF2_PROXY,
        (BF1_PROXY, "aggregator"): BF1_AGG,
        (BF2_PROXY, "aggregator"): BF2_AGG,
        (QF1_PROXY, "aggregator"): QF1_AGG,
        (BF1_AGG, "latestTimestamp"): int(now - 25 * 3600),   # primary всё равно остаётся
        (BF2_AGG, "latestTimestamp"): int(now - 25 * 3600),   # стейбл/мёртвая -> мимо
        (QF1_AGG, "latestTimestamp"): int(now - 60),          # market-нога -> в watch
    })
    primary, legs = feeds._resolve_legs(rpc, MID)
    assert primary == BF1_AGG
    assert legs == [BF1_AGG, QF1_AGG]


def test_resolve_legs_dedup_and_no_bf1():
    # (H2) QF1 резолвится в ТОТ ЖЕ агрегатор, что BF1 -> без дубля; нет BF1, но есть живая QF1 ->
    # рынок получает watch-ногу (primary=None)
    import time as _t
    now = _t.time()
    rpc = _FakeRpc({
        (_morpho_key(), "idToMarketParams"): ("L", "C", ORACLE2, "I", 0),
        (ORACLE2, "BASE_FEED_1"): BF1_PROXY,
        (ORACLE2, "QUOTE_FEED_1"): QF1_PROXY,
        (BF1_PROXY, "aggregator"): BF1_AGG,
        (QF1_PROXY, "aggregator"): BF1_AGG,                   # та же нога с двух геттеров
        (BF1_AGG, "latestTimestamp"): int(now - 60),
    })
    assert feeds._resolve_legs(rpc, MID) == (BF1_AGG, [BF1_AGG])
    rpc2 = _FakeRpc({
        (_morpho_key(), "idToMarketParams"): ("L", "C", ORACLE2, "I", 0),
        (ORACLE2, "QUOTE_FEED_1"): QF1_PROXY,                 # BASE_FEED_1 отсутствует
        (QF1_PROXY, "aggregator"): QF1_AGG,
        (QF1_AGG, "latestTimestamp"): int(now - 60),
    })
    assert feeds._resolve_legs(rpc2, MID) == (None, [QF1_AGG])


def test_per_leg_fallback_rpc():
    # (H3) 429/транспорт на ОДНОЙ ноге при живом остальном: вызов ретраится через fallback_rpc,
    # нога НЕ выпадает молча (дрейф карты 23<->25 ключей 07-23). Легитимный revert («нет
    # функции») фолбэк НЕ дёргает — ретрай ~60-80 отсутствующих ног на медленном публичном
    # эндпоинте раздувал резолв с ~80с до 6+ мин
    import time as _t
    now = _t.time()
    full = {
        (_morpho_key(), "idToMarketParams"): ("L", "C", ORACLE2, "I", 0),
        (ORACLE2, "BASE_FEED_1"): BF1_PROXY,
        (ORACLE2, "QUOTE_FEED_1"): QF1_PROXY,
        (BF1_PROXY, "aggregator"): BF1_AGG,
        (QF1_PROXY, "aggregator"): QF1_AGG,
        (BF1_AGG, "latestTimestamp"): int(now - 60),
        (QF1_AGG, "latestTimestamp"): int(now - 60),
    }
    partial = {k: v for k, v in full.items()
               if k in ((_morpho_key(), "idToMarketParams"), (ORACLE2, "BASE_FEED_1"), (BF1_PROXY, "aggregator"))}
    # 429-шторм (транспорт): QF1-цепочка и liveness падают HTTPError -> фолбэк добирает
    main = _FakeRpc(partial, transport_fail=True)
    fb = _FakeRpc(full)
    assert feeds._resolve_legs(main, MID, fallback_rpc=fb) == (BF1_AGG, [BF1_AGG, QF1_AGG])
    # без фолбэка та же деградация роняет QF1-ногу
    assert feeds._resolve_legs(main, MID) == (BF1_AGG, [BF1_AGG])
    # легитимный revert (не транспорт): фолбэк НЕ дёргается, нога честно отсутствует
    legit = _FakeRpc(partial, transport_fail=False)
    assert feeds._resolve_legs(legit, MID, fallback_rpc=fb) == (BF1_AGG, [BF1_AGG])
    # классификатор: транспорт по имени типа, revert - нет
    assert feeds._is_transport(HTTPError("429")) and not feeds._is_transport(Exception("execution reverted"))


def test_multileg_groups_and_primary_map():
    # (I) рынок числится под КАЖДОЙ ногой; primary_map отдаёт только BF1-ноги последнего резолва
    out = _patched_resolve({MID: (PLAIN_AGG, [PLAIN_AGG, QF1_AGG]), MID2: (QF1_AGG, [QF1_AGG])})
    assert sorted(out[QF1_AGG]) == sorted([MID, MID2])
    assert out[PLAIN_AGG] == [MID]
    pm = feeds.primary_map()
    assert pm == {MID: PLAIN_AGG, MID2: QF1_AGG}


def test_shadow_map_primary_guard():
    # (J) shadow-scale садится ТОЛЬКО на primary(BF1)-membership: cb-рынок под чужой ногой
    # (ожившая стейбл-нога в депег) НЕ должен получить свой scale на чужом агрегаторе;
    # primary=None = поведение до multi-leg (оба, как раньше)
    import chain.hotpath as hp

    class _M:
        def __init__(self, mid):
            self.market_id, self.loan_token, self.collateral_token = mid, "USDC", "cbBTC"

    fm = {BF1_AGG: [MID], QF1_AGG: [MID]}
    meta = {MID: ("0x" + "0e" * 20, 860000000000000000)}
    sm = hp._build_shadow_map(fm, meta, [_M(MID)], primary={MID: BF1_AGG})
    assert BF1_AGG in sm and QF1_AGG not in sm
    sm2 = hp._build_shadow_map(fm, meta, [_M(MID)])
    assert BF1_AGG in sm2 and QF1_AGG in sm2


def test_read_positions_fallback_swallows_transport():
    # (K) 429-шторм на confirmed-фолбэке НЕ убивает transmit-воркер (инцидент 15:06 23.07):
    # транспортная ошибка -> None (= f_norp у вызывающего), не исключение в треде
    import chain.hotpath as hp
    import chain.multicall as mc

    class _Rpc:
        def to_bytes32(self, x):
            return bytes.fromhex(x[2:])

    def _boom(*a, **kw):
        raise RuntimeError("429 Too Many Requests")

    # энкодеры стабим тоже: тест не должен требовать web3/eth_abi в окружении
    orig = (mc.aggregate3, mc.encode_market_call, mc.encode_position_call)
    mc.aggregate3, mc.encode_market_call, mc.encode_position_call = _boom, lambda b32: b"", lambda b32, b: b""
    try:
        assert hp.read_positions(_Rpc(), "0x" + "ab" * 32, ["0x" + "cd" * 20]) is None
    finally:
        mc.aggregate3, mc.encode_market_call, mc.encode_position_call = orig


def test_get_logs_fallback():
    # (L) 400 на primary (Alchemy-ключ №2 без getLogs) -> фолбэк-эндпоинт отвечает;
    # живой primary фолбэк не дёргает; отказ ВСЕХ -> проброс последней ошибки
    import chain.rpc as R

    class _W3:
        def __init__(self, fail, tag):
            self.fail, self.tag = fail, tag
            self.eth = self

        def get_logs(self, params):
            if self.fail:
                raise RuntimeError("400 Bad Request")
            return [{"src": self.tag}]

    orig = R._LOGS_FB
    R._LOGS_FB = [_W3(False, "fb")]
    try:
        assert R.get_logs_fb(_W3(True, "primary"), {})[0]["src"] == "fb"
        assert R.get_logs_fb(_W3(False, "primary"), {})[0]["src"] == "primary"
        R._LOGS_FB = [_W3(True, "fb")]
        try:
            R.get_logs_fb(_W3(True, "p"), {})
            raise AssertionError("должен был пробросить")
        except RuntimeError:
            pass
    finally:
        R._LOGS_FB = orig


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as e:                      # не только Assert: упавший импорт/тип = FAIL, не смерть раннера
                fails += 1
                print("FAIL", name, "%s: %s" % (type(e).__name__, e))
    sys.exit(1 if fails else 0)
