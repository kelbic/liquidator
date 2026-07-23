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


def _patched_resolve(mapping):
    """resolve_feeds поверх замоканного _resolve_one (I/O-шов из docstring)."""
    orig = feeds._resolve_one
    feeds._resolve_one = lambda rpc, mid: mapping.get(mid)
    try:
        return feeds.resolve_feeds(None, [_Mkt(m) for m in mapping])
    finally:
        feeds._resolve_one = orig


def test_override_substitutes_pattern():
    # (A) прямой вызов таблицы: композит -> паттерн, обычный адрес -> сам себя
    assert feeds.WATCH_PATTERN_OVERRIDES[COMPOSITE] == PATTERN
    assert feeds.WATCH_PATTERN_OVERRIDES.get(PLAIN_AGG, PLAIN_AGG) == PLAIN_AGG


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
    # (F) шторм на основном RPC (все None) -> пер-рыночный повтор через fallback_rpc;
    # легитимный None (не-Chainlink) остаётся None и на фолбэке
    MAIN, FB = object(), object()
    table = {(FB, MID): PATTERN, (FB, "0x" + "cd" * 32): PLAIN_AGG}   # main отдаёт None по всем
    orig = feeds._resolve_one
    feeds._resolve_one = lambda rpc, mid: table.get((rpc, mid))
    try:
        blind = feeds.resolve_feeds(MAIN, [_Mkt(MID), _Mkt("0x" + "cd" * 32), _Mkt("0x" + "ee" * 32)])
        assert blind == {}                                            # без фолбэка — слепота (инцидент 07-23)
        out = feeds.resolve_feeds(MAIN, [_Mkt(MID), _Mkt("0x" + "cd" * 32), _Mkt("0x" + "ee" * 32)],
                                  fallback_rpc=FB)
        assert out[PATTERN] == [MID] and out[PLAIN_AGG] == ["0x" + "cd" * 32]
        assert len(out) == 2                                          # 0xee-рынок легитимно вне карты
    finally:
        feeds._resolve_one = orig


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


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
