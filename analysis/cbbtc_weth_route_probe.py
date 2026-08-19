"""Read-only разведка прямого роута cbBTC->WETH (Base) — задача А после потери 50191667.

ЗАЧЕМ. Рынок 0x5dffffc7 = cbBTC(залог)/WETH(долг). Пары (cbBTC,WETH) в `_SWAP_ROUTES` НЕТ,
поэтому план цели жил на Kyber-агрегате: fast-lane невозможен, а для §9 (#133) такая цель
НЕДОСТУПНА ГРУППЕ по построению (Kyber-цели в same-pool не ходят — пул неизвестен).

ЧТО МЕРЯЕТСЯ. (1) какие пулы cbBTC/WETH вообще живы — UniV3 (fee-тиры) и Aerodrome Slipstream
(tick spacing), адреса берутся у ФАБРИК, не из памяти; (2) лестница глубины квотером обеих
венью; (3) обрыв и кап по правилу карты min(S_budget, S_обрыв/2).

ПОЗИТИВНЫЙ КОНТРОЛЬ (иначе тест умеет вернуть только ноль): implied-курс BTC/ETH из квоты
сверяется с тем же курсом, собранным ЧЕРЕЗ USDC двумя РАЗНЫМИ живыми роутами карты
(cbBTC->USDC и WETH->USDC). Расхождение > 3% = квотер/путь не тот, числа не читаются.

ЗАПУСК: RPC_URL=https://mainnet.base.org python3 analysis/cbbtc_weth_route_probe.py
Ноль записи, ноль секретов в вывод.
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

from chain.execute import _CBBTC, _UNIV3_USDC, _UNIV3_WETH, AERO_CL_ROUTER, _pack_path
from chain.route_depth import AERO_QUOTER, UNIV3_QUOTER

RPC = os.environ.get("RPC_URL", "https://mainnet.base.org")
UNIV3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"     # UniswapV3Factory (Base)
S = requests.Session()


def rpc(m, p, tries=6):
    last = None
    for _ in range(tries):
        r = S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                   timeout=120).json()
        if "error" not in r:
            return r["result"]
        last = r["error"]
        time.sleep(2.0)                       # публичный узел лимитирует — ждём, а не врём нулём
    raise RuntimeError("%s: %s" % (m, last))


def call(to, data):
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": data}, "latest"])[2:])


def sel(sig):
    return keccak(text=sig)[:4]


def try_call(to, data):
    try:
        return call(to, data)
    except Exception:
        return None


def quote(quoter, path, amount_in):
    # noqa: D401
    """quoteExactInput(bytes,uint256) -> amountOut. None = ступень ревертнула (пул не тянет)."""
    d = "0x" + (sel("quoteExactInput(bytes,uint256)")
                + abi_encode(["bytes", "uint256"], [path, int(amount_in)])).hex()
    r = try_call(quoter, d)
    return None if r is None or len(r) < 32 else int.from_bytes(r[:32], "big")


import functools
print = functools.partial(print, flush=True)   # вывод идёт в файл — буфер прячет прогресс

print("=" * 96)
print("РАЗВЕДКА cbBTC->WETH (Base). RPC: %s" % RPC)
print("=" * 96)

# --- 0. ПОЗИТИВНЫЙ КОНТРОЛЬ КУРСА через ДВА живых роута карты -----------------------------------
_ONE_BTC = 10 ** 8
_ONE_ETH = 10 ** 18
q_btc_usdc = quote(AERO_QUOTER, _pack_path(_CBBTC, 100, _UNIV3_USDC), _ONE_BTC // 10)      # 0.1 BTC
q_eth_usdc = quote(UNIV3_QUOTER, _pack_path(_UNIV3_WETH, 3000, _UNIV3_USDC), _ONE_ETH)     # 1 ETH
assert q_btc_usdc and q_eth_usdc, "живые роуты карты не котируются — стоп, дальше числа бессмысленны"
BTC_USD = q_btc_usdc / 1e6 * 10
ETH_USD = q_eth_usdc / 1e6
REF_RATIO = BTC_USD / ETH_USD
print("контроль: BTC=$%.0f (cbBTC->USDC), ETH=$%.0f (WETH->USDC) => BTC/ETH = %.4f"
      % (BTC_USD, ETH_USD, REF_RATIO))

# --- 1. ЖИВЫЕ ПУЛЫ cbBTC/WETH --------------------------------------------------------------------
print("\n1. ПУЛЫ (адреса — у фабрик, не из памяти)")
AERO_FACTORY = "0x" + call(AERO_CL_ROUTER, "0x" + sel("factory()").hex())[12:].hex()
print("   Slipstream factory (router.factory()): %s" % AERO_FACTORY)
POOLS = []          # (венью, параметр, адрес, ликвидность)
for fee in (100, 500, 3000, 10000):
    d = "0x" + (sel("getPool(address,address,uint24)")
                + abi_encode(["address", "address", "uint24"], [_CBBTC, _UNIV3_WETH, fee])).hex()
    r = try_call(UNIV3_FACTORY, d)
    a = "0x" + r[12:].hex() if r else None
    if a and int(a, 16):
        liq = int.from_bytes(call(a, "0x" + sel("liquidity()").hex()), "big")
        POOLS.append(("uni", fee, a, liq))
        print("   UniV3   fee=%-5d %s liquidity=%.4e" % (fee, a, liq))
for ts in (1, 50, 100, 200, 2000):
    d = "0x" + (sel("getPool(address,address,int24)")
                + abi_encode(["address", "address", "int24"], [_CBBTC, _UNIV3_WETH, ts])).hex()
    r = try_call(AERO_FACTORY, d)
    a = "0x" + r[12:].hex() if r else None
    if a and int(a, 16):
        liq = int.from_bytes(call(a, "0x" + sel("liquidity()").hex()), "big")
        POOLS.append(("aero", ts, a, liq))
        print("   Slipstr ts=%-5d %s liquidity=%.4e" % (ts, a, liq))
assert POOLS, "ни одного живого пула cbBTC/WETH — прямого роута не существует, стоп"

# --- 2. ЛЕСТНИЦА ГЛУБИНЫ ---------------------------------------------------------------------
# Размеры: от 0.05 cbBTC (цели этого рынка — 0.06-0.35 cbBTC) вверх до обрыва. Мелкий размер
# в лестнице обязателен: он даёт БАЗУ ratio (цена без импакта), от которой считается impact.
SIZES = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 50, 80, 120]
# Лестница ОБРЫВАЕТСЯ, как только impact съел бонус: дальше числа уже не влияют ни на выбор
# пула, ни на кап, а каждая ступень на холодном пуле стоит десятки секунд (форк тянет тики
# по одному через лимитируемый апстрим). Экономия здесь — не лень, а условие завершимости.
STOP_IMPACT = 3.0
print("\n2. ЛЕСТНИЦА (ratio = WETH_out / cbBTC_in; impact — от базы 0.05)")
BEST = None
for venue, par, addr, liq in POOLS:
    quoter = AERO_QUOTER if venue == "aero" else UNIV3_QUOTER
    path = _pack_path(_CBBTC, par, _UNIV3_WETH)
    row, base = [], None
    for s in SIZES:
        out = quote(quoter, path, int(s * _ONE_BTC))
        if out is None or out == 0:
            row.append((s, None, None))
            continue
        ratio = out / _ONE_ETH / s
        if base is None:
            base = ratio
        _imp = (1 - ratio / base) * 100.0
        row.append((s, ratio, _imp))
        if _imp >= STOP_IMPACT:
            break
    live = [r for r in row if r[1] is not None]
    if not live:
        print("   %s %-5s: ПУСТ (все ступени ревертнули)" % (venue, par))
        continue
    print("   %s %-5s (%s): база ratio=%.6f (контроль %.4f, дельта %.2f%%)"
          % (venue, par, addr[:10], live[0][1], REF_RATIO, (live[0][1] / REF_RATIO - 1) * 100))
    print("      " + "  ".join("%g->%s" % (s, "REVERT" if i is None else "%.2f%%" % i)
                               for s, _, i in row))
    if BEST is None or live[0][1] > BEST[0]:
        BEST = (live[0][1], venue, par, addr, row)

assert BEST, "ни один пул не котируется"
_ratio0, VENUE, PAR, ADDR, ROW = BEST
print("\n   ВЫБРАН: %s %s (%s) — лучшая цена на малом размере" % (VENUE, PAR, ADDR))
assert abs(_ratio0 / REF_RATIO - 1) < 0.03, (
    "ratio выбранного пула расходится с контрольным курсом > 3%% (%.4f vs %.4f) — путь/квотер "
    "не тот, числа не читаются" % (_ratio0, REF_RATIO))

# --- 3. ОБРЫВ И КАП ----------------------------------------------------------------------------
# Правило карты (24.07): кап = min(S_budget, S_обрыв/2), где S_обрыв — размер, на котором
# impact съедает ВЕСЬ бонус рынка (LIF-1). Делитель 2 — эмпирика дрейфа обрыва за сутки.
LIF_BONUS = 2.62          # рынок 0x5dffffc7, lltv 0.915 -> LIF 1.0262
S_BREAK = None
prev_s, prev_i = None, None
for s, ratio, imp in ROW:
    if imp is None:
        S_BREAK = prev_s
        break
    if imp >= LIF_BONUS:
        if prev_i is not None and prev_i < LIF_BONUS:      # линейная интерполяция между ступенями
            S_BREAK = prev_s + (s - prev_s) * (LIF_BONUS - prev_i) / (imp - prev_i)
        else:
            S_BREAK = s
        break
    prev_s, prev_i = s, imp
print("\n3. ОБРЫВ И КАП (бонус рынка 0x5dffffc7 = %.2f%%)" % LIF_BONUS)
if S_BREAK is None:
    print("   S_обрыв: НЕ ДОСТИГНУТ в пределах %g cbBTC (~$%.1fM)"
          % (SIZES[-1], SIZES[-1] * BTC_USD / 1e6))
    S_BREAK = SIZES[-1]
print("   S_обрыв ~ %.3f cbBTC (~$%.0f); правило S_обрыв/2 = %.3f cbBTC (~$%.0f)"
      % (S_BREAK, S_BREAK * BTC_USD, S_BREAK / 2, S_BREAK / 2 * BTC_USD))
for cap in (round(S_BREAK / 2, 3), round(S_BREAK / 4, 3)):
    out = quote(AERO_QUOTER if VENUE == "aero" else UNIV3_QUOTER,
                _pack_path(_CBBTC, PAR, _UNIV3_WETH), int(cap * _ONE_BTC))
    if out:
        imp = (1 - (out / _ONE_ETH / cap) / _ratio0) * 100
        print("   на капе %.3f cbBTC (~$%.0f): impact %.3f%% (запас к бонусу %.2fx)"
              % (cap, cap * BTC_USD, imp, LIF_BONUS / imp if imp > 0 else float("inf")))
