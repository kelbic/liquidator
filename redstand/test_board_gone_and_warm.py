"""Безнадёжная цель снимается, а склад набивается независимо от неё (#78).

Находка ревьюера 09.08. Две половины одного дефекта в пути доски:

1. `bs == 0` (позицию разобрали) возвращало "gone" и цель снималась, а `coll == 0` (долг есть,
   залога нет — брать нечего ни по какой цене) падало в общий `return None`: скамейка nobuild,
   пересборка по кругу, а в прогреве — ещё и `_arm_retry` по БЫСТРОМУ каденсу, предназначенному
   для «план МОГ бы встать». Асимметрия без причины: обе позиции одинаково мертвы.
   УТОЧНЕНИЕ ревьюера: «оставалась в списке навсегда» — переоценка. `dead` это локальный set
   окна, снятие действует на текущее окно, а не насовсем. Настоящая цена — жжёный каденс
   переспроса и неверная причина в журнале.

2. `self._engine()` жил ТОЛЬКО внутри ветки `isinstance(probe, dict)`. Так как _top_fat берёт
   k=1, ОДНА несобираемая цель на вершине жирного списка оставляла пул на пустых лестницах —
   те самые ~0.8с молчания на старте окна победы, ради устранения которых склад и набивается
   заранее. Прогрев патрона и подпись лестниц не зависят от того, собралась ли эта цель.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.multicall as mc                             # noqa: E402
import chain.spec_fire_live as sfl                       # noqa: E402

MID = "0x" + "11" * 32
BOR = "0x" + "22" * 20
FEED = "XRP"

# боевой рынок cbXRP/USDC: настоящие параметры, чтобы «gone» проверялся на живой арифметике
LOAN, COLL_T = "0x" + "aa" * 20, "0x" + "bb" * 20
ORACLE, IRM = "0x" + "cc" * 20, "0x" + "dd" * 20
LLTV = 625 * 10 ** 15
TBA, TBS = 35624351388041, 34399827107770773119
PRICE = 1013981470000000000000000000000000000
BS = 15927570158869477


def patch_chain(bs, coll_amt):
    """Подменяем ТОЛЬКО транспорт и декодеры multicall — сама _build исполняется настоящая."""
    mc.aggregate3 = lambda rpc, calls: [(True, b"")] * len(calls)
    mc.encode_id_to_market_params_call = lambda m: b""
    mc.encode_market_call = lambda m: b""
    mc.encode_position_call = lambda m, b: b""
    mc.encode_price_call = lambda: b""
    mc.decode_id_to_market_params = lambda d: (LOAN, COLL_T, ORACLE, IRM, LLTV)
    mc.decode_market = lambda d: (0, 0, TBA, TBS, 0, 0)
    mc.decode_position = lambda d: (bs, coll_amt)
    mc.decode_price = lambda d: PRICE


def mk_build():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._logs = []
    sf._log = sf._logs.append
    sf._now = lambda: 1000.0
    sf.rpc = type("R", (), {"to_bytes32": staticmethod(lambda m: b"\x00" * 32)})()
    sf.cfg = type("C", (), {"liquidator_address": "0x" + "ff" * 20,
                            "gas_limit_est": 260000,
                            "fast_lane_max_repaid_usd": 500000.0})()
    return sf


_saved = {k: getattr(mc, k) for k in
          ("aggregate3", "encode_id_to_market_params_call", "encode_market_call",
           "encode_position_call", "encode_price_call", "decode_id_to_market_params",
           "decode_market", "decode_position", "decode_price")}
try:
    # --- 1. ЗАЛОГА НЕТ -> "gone" (цель снимается), а не None (вечная пересборка) ----------
    patch_chain(BS, 0)
    assert mk_build()._build(MID, BOR, 16494.0) == "gone", \
        "позиция без залога не снята со списка — пересборка по кругу"

    # --- 2. КОНТРОЛЬ: разобранная позиция по-прежнему "gone" ------------------------------
    patch_chain(0, 10 ** 12)
    assert mk_build()._build(MID, BOR, 16494.0) == "gone", "поведение bs==0 сломано"

    # --- 3. КОНТРОЛЬ: живая позиция НЕ снимается (иначе п.1 неотличим от «всегда gone») ---
    # Роута под синтетический токен нет, поэтому здоровый исход здесь — None («не собрали»),
    # но именно None, а не "gone": цель обязана остаться в списке.
    patch_chain(BS, 25944964342)
    assert mk_build()._build(MID, BOR, 16494.0) is None, \
        "живую цель сняли со списка как безнадёжную"
finally:
    for k, v in _saved.items():
        setattr(mc, k, v)

print("OK 1/3: залога нет -> LOSS-стоп; разобранная -> LOSS-стоп; живая остаётся в списке")


# --- 4. СКЛАД НАБИВАЕТСЯ, даже если верхняя цель не собралась ------------------------------
def mk_prearm(build_result):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: 5000.0
    sf._logs = []
    sf._log = sf._logs.append
    sf.probe_board = {}
    sf.pending_fire = {}
    sf._ws_handoff_ts = {}
    sf._board_next_try = {}
    sf._board_probe_cache = {}
    sf._board = {}
    sf.stats = {}
    sf.warmed = []
    sf._engine = lambda: sf.warmed.append(1)
    sf._arm_board = lambda p, m, b, f, debt=0.0: True
    sf._top_fat = lambda feed, direction, max_dist=None, k=1: ([(MID, BOR, 31000.0)], None, None)
    sf._board_ready = lambda m, b: False
    sf._build_fn = lambda m, b, d: build_result
    return sf


sf = mk_prearm(None)                       # верхняя цель не собралась (роут/жир/безнадёга)
sf._prearm_board(FEED, "down", 0.005)
assert sf.warmed, "склад не набит из-за несобираемой цели — пул выйдет на пустые лестницы"
assert sf._board_next_try.get(FEED), "осечка арма не поставлена на повтор"

sf2 = mk_prearm({"calldata": "0xabcd", "gas": 260000})   # КОНТРОЛЬ: собралась — тоже набит
sf2._prearm_board(FEED, "down", 0.005)
assert sf2.warmed, "склад не набит на успешной сборке — сломан прежний путь"
assert FEED in sf2.probe_board, "доска не опубликована (регрессия #47)"
print("OK 2/3: прогрев склада не зависит от сборки верхней цели (k=1 больше не глушит пул)")

# --- 5. МЁРТВАЯ цель не жжёт быстрый переспрос (хвост #78, находка ревьюера) --------------
# Быстрый переспрос #57 существует для случая «план МОГ бы встать» (сеть моргнула, setPlan не
# сел). У цели, где брать нечего, он не даст ничего ни сейчас, ни через 5с — а в журнал уходила
# неверная причина «проба не собралась». Склад при этом обязан набиваться по-прежнему.
sf3 = mk_prearm("gone")
sf3._prearm_board(FEED, "down", 0.005)
assert sf3.warmed, "мёртвая верхняя цель снова оставила пул на пустых лестницах"
assert not sf3._board_next_try.get(FEED), \
    "мёртвая цель поставлена на БЫСТРЫЙ переспрос — каденс жжётся впустую"
assert any("мертва" in s for s in sf3._logs), "причина в журнале не названа"
# и КОНТРОЛЬ: настоящая осечка сборки переспрос ВЗВОДИТ (иначе п.5 просто ломает #57)
assert mk_prearm(None) is not None
_sf = mk_prearm(None); _sf._prearm_board(FEED, "down", 0.005)
assert _sf._board_next_try.get(FEED), "осечка сборки больше не переспрашивается — сломан #57"
print("OK 3/3: мёртвая цель не жжёт быстрый переспрос, осечка сборки — жжёт (#57 цел)")

print("=== #78 ЗАКРЫТ ===")
