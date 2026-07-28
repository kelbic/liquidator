"""БУДИЛЬНИК USDe/USDC (27.07) — ОТДЕЛЬНЫЙ СЕРВИС, НЕ hot path.

Почему отдельно: оракул `0xF4b17C79` — байткод-константа $1.00 (codesize 45, price()=1e24,
без owner/прокси/фидов), цена не двинется НИКОГДА ⇒ флип приходит только от начисления
процентов. Это таймер, а не гонка: латентность не нужна, transmit-детектор бесполезен,
predict бесполезен. В covered рынок НЕ добавляем — иначе бот попытается ликвидировать
позицию ЦЕЛИКОМ ($20M), а выход держит лишь ~$0.5M (замер 27.07: 2M USDe = импакт 40%).

Что делает: раз в час читает рынок + top-N позиций (1 aggregate3, ~26 CU), считает
детерминированную ETA = ln(HF)/APR, оценивает КАП СРЕЗА по реальной глубине пула и шлёт
TG только на СМЕНУ СОСТОЯНИЯ (приближение / флип / прыжок APR / уход ликвидности лендеров).

Триггеры тревоги:
  * HF <= 1.0        -> ФЛИП: ликвидируем срезами по капу (исполнение вручную — slice-логики в
                        боевом контракте пока НЕТ, он репеит позицию целиком);
  * HF <= 1.002      -> на пороге (дни), перейти на ручное наблюдение;
  * APR вырос >=1.5x -> лендеры выводят ликвидность, ETA сжимается БЕЗ предупреждения;
  * ликвидность рынка < LIQ_WARN -> тот же сигнал с другой стороны.

  python analysis/usde_watch.py            # разовый прогон (печать + TG на смену состояния)
  python analysis/usde_watch.py --quiet    # только TG, без печати
"""
import argparse
import json
import math
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from chain.rpc import BaseRpc
from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
                             encode_market_call, decode_market, encode_position_call, decode_position,
                             encode_price_call, decode_price)
from chain.simulate import to_assets_up
from alerts import Alerter
from eth_abi import encode as abi_encode, decode as abi_decode
from eth_utils import keccak

MID = "0x54cf9be57fdfa6457a660991907434ff9d295c465a603a50126ff647d50b7354"
USDE = "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
IRM_SEL = keccak(text="borrowRateView((address,address,address,address,uint256),"
                      "(uint128,uint128,uint128,uint128,uint128,uint128))")[:4]
STATE = os.environ.get("USDE_WATCH_STATE", "usde_watch_state.json")
HF_FLIP, HF_NEAR = 1.0, float(os.environ.get("USDE_HF_NEAR", "1.002"))
APR_JUMP = float(os.environ.get("USDE_APR_JUMP", "1.5"))
LIQ_WARN = float(os.environ.get("USDE_LIQ_WARN_USD", "5e6"))
SLICE_MAX_IMPACT = float(os.environ.get("USDE_SLICE_MAX_IMPACT", "0.01"))   # доля от бонуса LIF-1
LIQ_CURSOR, MAX_LIF = 0.3, 1.15        # Morpho Blue: LIF = min(MAX_LIF, 1/(1-CURSOR*(1-LLTV)))


def lif_of(lltv_wad: int) -> float:
    """Бонус ликвидатора рынка. Чистая. 28.07: было зашито 2.5% — на LLTV 0.915 реально 2.617%."""
    return min(MAX_LIF, 1.0 / (1.0 - LIQ_CURSOR * (1.0 - lltv_wad / 1e18)))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}


def slice_cap_usd(probe=(250_000, 500_000, 1_000_000, 1_150_000, 1_300_000, 2_000_000)):
    """КАП СРЕЗА по живой глубине -> (cap_usd, impact). Наибольший размер с импактом <=
    SLICE_MAX_IMPACT, ВМЕСТЕ с его фактическим импактом (28.07: на капе он 0.22%, а не
    потолочный 1% — считать приз по потолку значит занижать его в полтора раза).
    Обязателен: полный репей кита ($20M) при выходе ~$1.2M = гарантированный минус.
    Сетка доведена до обрыва маршрута (замер 28.07: выход упирается в ~$1.2M)."""
    best, best_imp = 0.0, 0.0
    for usd in probe:
        url = ("https://aggregator-api.kyberswap.com/base/api/v1/routes"
               "?tokenIn=%s&tokenOut=%s&amountIn=%d" % (USDE, USDC, int(usd * 10 ** 18)))
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))
            out = int(((r.get("data") or {}).get("routeSummary") or {}).get("amountOut") or 0) / 1e6
        except Exception:
            continue
        if out and (1 - out / usd) <= SLICE_MAX_IMPACT:
            best, best_imp = usd, (1 - out / usd)
    return best, best_imp


def slice_prize(cap_usd: float, lif: float, impact: float = None) -> float:
    """Нетто среза: бонус минус ФАКТИЧЕСКИЙ импакт выхода (не потолочный — см. slice_cap_usd).
    impact=None -> считаем по потолку (консервативный пол). Чистая."""
    imp = SLICE_MAX_IMPACT if impact is None else float(impact)
    return cap_usd * lif * (1.0 - imp) - cap_usd


def heal_after(cap_usd: float, debt_usd: float, lif: float, lltv: float) -> float:
    """Насколько ВЫРАСТЕТ HF после среза: LIF*LLTV<1 => ликвидация ЛЕЧИТ позицию. Чистая."""
    if debt_usd <= cap_usd:
        return 0.0
    return (debt_usd - cap_usd * lif * lltv) / (debt_usd - cap_usd) - 1.0


def heal_days(cap_usd: float, debt_usd: float, lif: float, lltv: float, apr: float) -> float:
    """Через сколько дней позиция вернётся под 1.0 после среза (тот же таймер процентов)."""
    h = heal_after(cap_usd, debt_usd, lif, lltv)
    return math.log(1.0 + h) / apr * 365 if h > 0 and apr > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    cfg = Config.from_env()
    # 28.07: фолбэк на основной RPC. Часовой прогон сидел ТОЛЬКО на bg-ключе (dRPC) и падал
    # с 429 -> сторож главного приза молча не работал (нашли при проверке ревьюверских цифр).
    # Цена страховки: ~26 CU/час на Alchemy = шум против его квоты, зато будильник не слепнет.
    _bg = os.environ.get("BG_RPC_URL") or cfg.rpc_url
    rpc = BaseRpc(_bg, cfg.chain_id)
    try:
        rpc._web3().eth.block_number
    except Exception as e:
        print("bg-RPC отказал (%s) -> перехожу на основной" % type(e).__name__)
        rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    w3 = rpc._web3()
    m32 = rpc.to_bytes32(MID)
    r = aggregate3(rpc, [(MORPHO, encode_id_to_market_params_call(m32)), (MORPHO, encode_market_call(m32))])
    loan, coll, oracle, irm, lltv = decode_id_to_market_params(r[0][1])
    mk = decode_market(r[1][1])
    px = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
    args = abi_encode(["(address,address,address,address,uint256)",
                       "(uint128,uint128,uint128,uint128,uint128,uint128)"],
                      [(loan, coll, oracle, irm, lltv), (mk[0], mk[1], mk[2], mk[3], mk[4], mk[5])])
    apr = abi_decode(["uint256"], w3.eth.call(
        {"to": w3.to_checksum_address(irm), "data": IRM_SEL + args}))[0] / 1e18 * 365 * 86400
    lif = lif_of(lltv)
    supply, borrow = mk[0] / 1e6, mk[2] / 1e6
    liq = supply - borrow
    # top-N позиций: список адресов держим в state (обновляется API-прогоном flip_eta/verify)
    try:
        prev = json.load(open(STATE))
    except Exception:
        prev = {}
    addrs = prev.get("addrs") or []
    if not addrs:
        Q = ("query P($k:[String!]){ marketPositions(first:20, orderBy:BorrowShares, "
             "orderDirection:Desc, where:{marketUniqueKey_in:$k, chainId_in:[8453]}){ "
             "items{ user{address} } } }")
        try:
            req = urllib.request.Request("https://blue-api.morpho.org/graphql",
                data=json.dumps({"query": Q, "variables": {"k": [MID]}}).encode(),
                headers={"Content-Type": "application/json", **UA})
            d = json.load(urllib.request.urlopen(req, timeout=30))
            addrs = [p["user"]["address"] for p in d["data"]["marketPositions"]["items"]]
        except Exception as e:
            print("не смог получить список позиций: %s" % type(e).__name__)
            return
    res = aggregate3(rpc, [(MORPHO, encode_position_call(m32, b)) for b in addrs[:a.top]])
    rows = []
    for b, (ok, data) in zip(addrs[:a.top], res):
        if not (ok and data):
            continue
        bs, col = decode_position(data)
        if bs == 0:
            continue
        debt = to_assets_up(int(bs), mk[2], mk[3])
        hf = (col * px // 10 ** 36) * (lltv / 1e18) / debt
        eta = math.log(hf) / apr * 365 if hf > 1 and apr > 0 else 0.0
        rows.append({"a": b, "debt": debt / 1e6, "hf": hf, "eta": eta})
    rows.sort(key=lambda x: x["hf"])
    cap, cap_imp = slice_cap_usd()
    if not a.quiet:
        print("USDe/USDC: долг $%.1fM supply $%.1fM ЛИКВИДНОСТЬ ЛЕНДЕРОВ $%.1fM | APR %.2f%%"
              % (borrow / 1e6, supply / 1e6, liq / 1e6, apr * 100))
        print("КАП СРЕЗА по глубине пула (импакт<=%.1f%%): $%.0fk | приз ~$%.1fk "
              "(LIF %.3f%%, минус импакт) | лечение после среза: HF+%.4f"
              % (SLICE_MAX_IMPACT * 100, cap / 1e3, slice_prize(cap, lif, cap_imp) / 1e3,
                 (lif - 1) * 100, heal_after(cap, rows[0]["debt"], lif, lltv / 1e18)
                 if rows else 0.0))
        for x in rows:
            print("   $%8.1fM HF=%.5f ETA=%5.0f дней  %s" % (x["debt"] / 1e6, x["hf"], x["eta"], x["a"][:12]))
    # ---- тревоги только на СМЕНУ состояния ----
    alerts = []
    for x in rows:
        key = x["a"].lower()
        was = (prev.get("hf") or {}).get(key)
        if x["hf"] <= HF_FLIP:
            alerts.append("ФЛИП USDe $%.1fM HF=%.4f — СРЕЗ $%.0fk, приз ~$%.1fk; полный репей НЕЛЬЗЯ "
                          "(выход держит ~$%.0fk). ОДИН срез лечит позицию на %.0f дней — "
                          "второго шанса сегодня не будет"
                          % (x["debt"] / 1e6, x["hf"], cap / 1e3, slice_prize(cap, lif, cap_imp) / 1e3, cap / 1e3,
                             heal_days(cap, x["debt"], lif, lltv / 1e18, apr)))
        elif x["hf"] <= HF_NEAR and (was is None or was > HF_NEAR):
            alerts.append("USDe $%.1fM подошёл к порогу: HF=%.5f, ETA %.0f дней — ручное наблюдение"
                          % (x["debt"] / 1e6, x["hf"], x["eta"]))
    apr_was = prev.get("apr")
    if apr_was and apr >= apr_was * APR_JUMP:
        alerts.append("USDe APR %.2f%% -> %.2f%% (×%.1f): лендеры выводят, ETA сжимается"
                      % (apr_was * 100, apr * 100, apr / apr_was))
    if liq < LIQ_WARN and (prev.get("liq") is None or prev["liq"] >= LIQ_WARN):
        alerts.append("USDe ликвидность лендеров $%.1fM < порога $%.1fM — util к 100%%, APR прыгнет"
                      % (liq / 1e6, LIQ_WARN / 1e6))
    if alerts:
        msg = "⏳ USDe-будильник: " + " | ".join(alerts)
        if not a.quiet:
            print("\n" + msg)
        try:
            Alerter(cfg.tg_bot_token, cfg.tg_admin_id, antiflood_sec=0, tag=cfg.bot_tag).send(
                msg, key="usde:%s" % (rows[0]["a"][:10] if rows else "x"))
        except Exception:
            pass
    json.dump({"addrs": addrs, "apr": apr, "liq": liq,
               "hf": {x["a"].lower(): x["hf"] for x in rows}}, open(STATE, "w"))
    # Пульс ТОЛЬКО на успешном цикле: упавший на 429 прогон не бьётся — heartbeat-check увидит
    # ровно ту поломку, что молчала весь день 28.07.
    from heartbeat import beat
    beat("usde-watch")


if __name__ == "__main__":
    main()
