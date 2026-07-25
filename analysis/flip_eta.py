"""ETA ФЛИПА для близких целей (25.07, доворот ревьювера): «ждём» должно быть ЧИСЛОМ.

Для каждой жирной цели у края считаем ДВЕ скорости и их разницу:
  - долг растёт: borrow APR из IRM рынка (borrowRateView при текущей утилизации);
  - залог растёт: дрейф ОРАКУЛА рынка по истории (для rate-оракулов вроде
    wstETH-stETH Exchange Rate это стейкинг-доходность; для рыночных фидов — шум, не тренд).
  net = oracle_drift − borrow_APR. net<0 -> HF падает, ETA = ln(HF)/|net|. net>0 -> цель
  УХОДИТ от ликвидации (флип от процентов не придёт; нужен рыночный триггер).

Открытие 25.07: wstETH-кит $1.6M — оракул +2.2%/год против долга 1.73% => HF РАСТЁТ, ETA нет.
Именно поэтому «wstETH следующий» было неверно, а стоячий огонь по нему строить нельзя.

  python analysis/flip_eta.py                # топ-10 по долгу среди HF<=1.05
  python analysis/flip_eta.py --hf 1.02 --n 20
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from chain.rpc import BaseRpc
from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
                             encode_market_call, decode_market, encode_price_call, decode_price,
                             encode_position_call, decode_position)
from chain.simulate import to_assets_up
from strategy.scanner import load_covered_markets
from eth_abi import encode as abi_encode, decode as abi_decode
from eth_utils import keccak

BLOCKS_PER_DAY = 43200          # Base: 2с/блок
IRM_SEL = keccak(text="borrowRateView((address,address,address,address,uint256),"
                      "(uint128,uint128,uint128,uint128,uint128,uint128))")[:4]


def oracle_drift_apr(w3, oracle, bn, days=30):
    """Годовой дрейф цены оракула по истории. None = архив недоступен."""
    try:
        sel = keccak(text="price()")[:4]
        now = int(w3.eth.call({"to": w3.to_checksum_address(oracle), "data": sel}, bn).hex(), 16)
        old = int(w3.eth.call({"to": w3.to_checksum_address(oracle), "data": sel},
                              bn - days * BLOCKS_PER_DAY).hex(), 16)
        if not old:
            return None
        return (now / old - 1) * 365.0 / days
    except Exception:
        return None


MARGIN_WARN = float(os.environ.get("RATE_MARGIN_WARN", "0.005"))   # 0.5%/год: запас до смены знака
STATE_PATH = os.environ.get("FLIP_ETA_STATE", "flip_eta_state.json")


def compute_rows(cfg, hf_max=1.05, n=10, days=30):
    """[{pair, borrower, debt_usd, hf, apr, drift, margin, market_oracle}] — переиспользуемо
    (дневная сводка зовёт это же). margin = дрейф оракула − APR долга: ОСЬ НАБЛЮДЕНИЯ для
    rate-класса (ревью 25.07): пока margin>0 цель уходит вверх, смена знака = цель ожила.
    APR не константа — Morpho IRM адаптивен, за изломом утилизации (~90%) ставка растёт кратно."""
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    w3 = rpc._web3()
    MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
    bn = w3.eth.block_number
    by_id = {m.market_id: m for m in load_covered_markets(cfg.covered_markets_path)}
    snap = json.load(open(cfg.candidates_snapshot_path))
    cands = []
    for key, hf in (snap.get("hf_by") or {}).items():
        mid, b = key.split("|", 1)
        if hf and hf <= hf_max and mid in by_id:
            cands.append((snap["debt_by"].get(key, 0.0), mid, b))
    cands.sort(reverse=True)
    out, drift_cache = [], {}
    for debt_usd, mid, b in cands[:n]:
        m = by_id[mid]
        m32 = rpc.to_bytes32(mid)
        try:
            r = aggregate3(rpc, [(MORPHO, encode_id_to_market_params_call(m32)),
                                 (MORPHO, encode_market_call(m32)),
                                 (MORPHO, encode_position_call(m32, b))])
            loan, coll, oracle, irm, lltv = decode_id_to_market_params(r[0][1])
            mk = decode_market(r[1][1])
            bs, col = decode_position(r[2][1])
            if bs == 0:
                continue
            px = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
            debt = to_assets_up(int(bs), mk[2], mk[3])
            hf = (col * px // 10 ** 36) * (lltv / 1e18) / debt
            args = abi_encode(["(address,address,address,address,uint256)",
                               "(uint128,uint128,uint128,uint128,uint128,uint128)"],
                              [(loan, coll, oracle, irm, lltv),
                               (mk[0], mk[1], mk[2], mk[3], mk[4], mk[5])])
            apr = abi_decode(["uint256"], w3.eth.call(
                {"to": w3.to_checksum_address(irm), "data": IRM_SEL + args}))[0] / 1e18 * 365 * 86400
        except Exception:
            continue
        if oracle not in drift_cache:
            drift_cache[oracle] = oracle_drift_apr(w3, oracle, bn, days)
        drift = drift_cache[oracle]
        out.append({"pair": "%s/%s" % (m.collateral_token, m.loan_token), "borrower": b,
                    "debt_usd": debt_usd, "hf": hf, "apr": apr, "drift": drift,
                    "margin": (drift - apr) if drift is not None else None,
                    "market_oracle": drift is not None and abs(drift) > 0.10,
                    "key": "%s|%s" % (mid[:10], b[:8])})
    return out


def margin_changes(rows, path=STATE_PATH):
    """Сравнить margin с прошлым прогоном: [(key, pair, было, стало, тип)] — СМЕНА ЗНАКА или
    падение запаса ниже MARGIN_WARN. Пишет новое состояние. Это и есть вотчер класса."""
    try:
        prev = json.load(open(path))
    except Exception:
        prev = {}
    changes, cur = [], {}
    for r in rows:
        if r["market_oracle"] or r["margin"] is None:
            continue                                    # ось имеет смысл только для rate-оракулов
        cur[r["key"]] = r["margin"]
        was = prev.get(r["key"])
        if was is not None and was > 0 >= r["margin"]:
            changes.append((r["key"], r["pair"], was, r["margin"], "ЗНАК СМЕНИЛСЯ — цель ожила"))
        elif r["margin"] < 0:
            # запас отрицательный = HF реально падает, ETA существует (см. таблицу в flip_eta)
            changes.append((r["key"], r["pair"], was, r["margin"], "запас отрицательный — ETA есть"))
        # 0 < margin < MARGIN_WARN — «тонко, но уходит вверх»: видно в таблице, кричать не о чем
    try:
        json.dump(cur, open(path, "w"))
    except Exception:
        pass
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hf", type=float, default=1.05)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--days", type=int, default=30, help="окно замера дрейфа оракула")
    a = ap.parse_args()

    cfg = Config.from_env()
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    w3 = rpc._web3()
    MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
    bn = w3.eth.block_number
    by_id = {m.market_id: m for m in load_covered_markets(cfg.covered_markets_path)}
    snap = json.load(open(cfg.candidates_snapshot_path))
    rows = []
    for key, hf in (snap.get("hf_by") or {}).items():
        mid, b = key.split("|", 1)
        if hf and hf <= a.hf and mid in by_id:
            rows.append((snap["debt_by"].get(key, 0.0), mid, b, hf))
    rows.sort(reverse=True)
    rows = rows[:a.n]
    if not rows:
        print("нет целей под HF<=%.3f (снапшот пуст? hf_by появляется после первого refresh)" % a.hf)
        return

    print("ETA ФЛИПА (блок %d, дрейф оракула за %dд)" % (bn, a.days))
    print("%-15s %-10s %10s %8s %7s %7s  %s" % ("пара", "заёмщик", "долг$", "HF", "APR%", "фид%", "вывод"))
    drift_cache = {}
    for debt_usd, mid, b, _hf_api in rows:
        m = by_id[mid]
        m32 = rpc.to_bytes32(mid)
        pair = "%s/%s" % (m.collateral_token, m.loan_token)
        try:
            r = aggregate3(rpc, [(MORPHO, encode_id_to_market_params_call(m32)),
                                 (MORPHO, encode_market_call(m32)),
                                 (MORPHO, encode_position_call(m32, b))])
            loan, coll, oracle, irm, lltv = decode_id_to_market_params(r[0][1])
            mk = decode_market(r[1][1])
            bs, col = decode_position(r[2][1])
            if bs == 0:
                continue
            px = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
            debt = to_assets_up(int(bs), mk[2], mk[3])
            hf = (col * px // 10 ** 36) * (lltv / 1e18) / debt
            args = abi_encode(["(address,address,address,address,uint256)",
                               "(uint128,uint128,uint128,uint128,uint128,uint128)"],
                              [(loan, coll, oracle, irm, lltv),
                               (mk[0], mk[1], mk[2], mk[3], mk[4], mk[5])])
            rate_ps = abi_decode(["uint256"], w3.eth.call(
                {"to": w3.to_checksum_address(irm), "data": IRM_SEL + args}))[0] / 1e18
            apr = rate_ps * 365 * 86400
        except Exception as e:
            print("%-15s %-10s  ОШИБКА %s" % (pair, b[:8], type(e).__name__))
            continue
        if oracle not in drift_cache:
            drift_cache[oracle] = oracle_drift_apr(w3, oracle, bn, a.days)
        drift = drift_cache[oracle]
        net = (drift if drift is not None else 0.0) - apr
        # РАЗВИЛКА ЧЕСТНОСТИ: |дрейф| > MARKET_DRIFT_CUT => оракул РЫНОЧНЫЙ, и «дрейф» это ТРЕНД
        # цены (шум, экстраполяция), а не детерминированное начисление. ETA по нему — не обещание.
        _market_oracle = drift is not None and abs(drift) > 0.10
        if hf <= 1:
            verdict = "УЖЕ ликвидируем"
        elif _market_oracle:
            verdict = ("рыночный оракул: тренд %+.0f%%/год -> при его сохранении ~%.0f дней "
                       "(ЭКСТРАПОЛЯЦИЯ, не начисление)"
                       % (drift * 100, max(0.0, math.log(hf) / max(1e-9, -net) * 365)) if net < 0
                       else "рыночный оракул: тренд %+.0f%%/год вверх" % (drift * 100))
        elif net < 0:
            verdict = "ETA %.0f дней (детерминировано: начисление)" % (math.log(hf) / (-net) * 365)
        else:
            verdict = "УХОДИТ ВВЕРХ (+%.2f%%/год) — флип только от рыночного события" % (net * 100)
        print("%-15s %-10s %10.0f %8.4f %7.2f %7s  %s" % (
            pair, b[:8], debt_usd, hf, apr * 100,
            ("%.2f" % (drift * 100)) if drift is not None else "н/д", verdict))


if __name__ == "__main__":
    main()
