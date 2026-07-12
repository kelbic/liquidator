"""Read-only: глубина маршрутов LBTC->cbBTC на UniV3 (Base) через QuoterV2.
Самокалибровка: сперва воспроизводим известную квоту cbBTC->WETH(500)->USDC(500) (CBBTC_PLAN:
$2k -> ~+0.45% номинала, impact ~0.3%) — подтверждает адрес квотера. Затем LBTC->cbBTC.
Первоисточники: LBTC-адрес из Morpho idToMarketParams (не из памяти). Ноль записи, ноль секретов в вывод."""
import os, sys
sys.path.insert(0, "/root/liquidator")
from web3 import Web3
from chain.execute import _pack_path, _CBBTC, _UNIV3_WETH, _UNIV3_USDC

MID = "0x4944a1169bc07b441473b830308ffe5bb535c10a9f824e33988b60738120c48e"  # LBTC/cbBTC
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"   # UniV3 QuoterV2 (Base) — валидируется сан-квотой

w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
assert w3.is_connected(), "RPC недоступен"

# --- LBTC-адрес из первоисточника: idToMarketParams(mid) -> (loan, collat, oracle, irm, lltv) ---
res = w3.eth.call({"to": MORPHO, "data": "0x2c3c9157" + MID[2:]})
loan = "0x" + res[12:32].hex()
collat = "0x" + res[44:64].hex()
assert loan.lower() == _CBBTC.lower(), f"loan != cbBTC: {loan}"
LBTC = Web3.to_checksum_address(collat)
CBBTC = Web3.to_checksum_address(_CBBTC)
dec = lambda t: int.from_bytes(w3.eth.call({"to": Web3.to_checksum_address(t), "data": "0x313ce567"})[-32:], "big")
d_lbtc, d_cbbtc = dec(LBTC), dec(_CBBTC)
print(f"LBTC={LBTC} (dec={d_lbtc}), cbBTC dec={d_cbbtc}")

def quote(path: bytes, amount_in: int):
    """QuoterV2.quoteExactInput(bytes,uint256) -> amountOut (первое слово ответа)."""
    from eth_abi import encode
    data = bytes.fromhex("cdca1753") + encode(["bytes", "uint256"], [path, amount_in])
    try:
        r = w3.eth.call({"to": QUOTER_V2, "data": "0x" + data.hex()})
        return int.from_bytes(r[:32], "big")
    except Exception:
        return None

# --- сан-квота: cbBTC(0.018)->WETH(500)->USDC(500) должна дать ~$2k ---
san = quote(_pack_path(_CBBTC, 500, _UNIV3_WETH, 500, _UNIV3_USDC), int(0.018 * 10**d_cbbtc))
assert san and san > 0, "сан-квота ревертнула — адрес квотера неверен, стоп"
btc_usd = (san / 10**6) / 0.018
print(f"сан-квота ok: известный маршрут жив; implied BTC=${btc_usd:,.0f}")
assert 20_000 < btc_usd < 300_000, f"неправдоподобная цена BTC {btc_usd}"

# --- маршруты LBTC->cbBTC ---
routes = {
    "direct fee=100": _pack_path(LBTC, 100, _CBBTC),
    "direct fee=500": _pack_path(LBTC, 500, _CBBTC),
    "direct fee=3000": _pack_path(LBTC, 3000, _CBBTC),
    "2hop WETH 500/500": _pack_path(LBTC, 500, _UNIV3_WETH, 500, _CBBTC),
    "2hop WETH 3000/500": _pack_path(LBTC, 3000, _UNIV3_WETH, 500, _CBBTC),
}
targets_usd = [1_500, 56_000, 150_000, 380_000]   # 380k = сиз позиции $350.8k долга + бонус 2.6%
sizes_lbtc = [round(u / btc_usd, 3) for u in targets_usd]
print(f"\nразмеры: " + ", ".join(f"{s}L(~${u/1000:.0f}k)" for s, u in zip(sizes_lbtc, targets_usd)))
print(f"{'маршрут':22s} | " + " | ".join(f"{s:>7g} LBTC" for s in sizes_lbtc) + "  (выход cbBTC/вход LBTC, паритет~1.0)")
for name, path in routes.items():
    ratios = []
    for s in sizes_lbtc:
        ain = int(s * 10**d_lbtc)
        out = quote(path, ain)
        ratios.append(None if out is None else out / 10**d_cbbtc / s)
    cells = " | ".join("   пусто  " if r is None else f"{r:.5f}" for r in ratios)
    base = ratios[0]
    impact = "" if (base is None or ratios[-1] is None) else f"  impact@${targets_usd[-1]//1000}k={100*(1-ratios[-1]/base):.2f}%"
    print(f"{name:22s} | {cells}{impact}")
