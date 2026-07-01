"""Read-only close-out probe — are the Base pre-liq contracts on cbXRP/cbADA actually USED?

Registry: 11 pre-liq contracts on Base, 0 on our 40 markets. A deployed contract is NOT usage —
opt-in is on-chain authorization (Morpho.isAuthorized(borrower, preLiqContract)), which the API
does not expose. This probes the two FIXED-LIF (preLIF1==preLIF2, ~7.41% bonus) contracts on
FAMILIAR assets (cbXRP/USDC, cbADA/USDC): does ANY live borrower opt in, and sit at/near its
pre-liq threshold (LTV >= preLltv, computed on-chain via the contract's OWN preLiquidationOracle)?

Reuses the bot's proven helpers (chain.simulate exact integer math + ABIs, chain.rpc, chain.morpho
API query) so the numbers match production. READ-ONLY: eth_call only, never reads WALLET_KEY, sends
no tx, writes no files, does not touch the running bot. A handful of eth_calls per contract.

    python -m analysis.preliq_optin_probe        # from repo root, prod venv
"""
from __future__ import annotations
import sys

# Verified on-chain in a prior session. preLltv/oracle are the pre-liq contract's OWN params.
TARGETS = [
    {"pair": "cbXRP/USDC",
     "market_id": "0xfdfecf85a4dd90a7637ae2aaf28b35061166f0e62bfc714c565eed9f7e959783",
     "preliq": "0x5C8455b4D2ECc24e9807032920101794e90d56e8",
     "preliq_oracle": "0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE",
     "pre_lltv_wad": 727366070175296029},
    {"pair": "cbADA/USDC",
     "market_id": "0xf2a59ad1aec67664c564cb33c33538ab41372363204ce2bdcf00900b7d28c6ab",
     "preliq": "0x8e9d70ecDc6d1155e92CFd822CB29B2e1251C06c",
     "preliq_oracle": "0x35D87a743D1F2f7CaFb42D855dC1c5Df857Ce45f",
     "pre_lltv_wad": 727366070175296029},
]
MIN_DEBT_USD = 100.0   # ignore dust borrowers (no pre-liq profit possible regardless)
NEAR_FRAC = 0.97       # 'near' = LTV within ~3% below preLltv (pre-liq HF in [1, ~1.031])
MAX_CHECK = 300        # safety cap on isAuthorized probes per market


def main():
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import (MORPHO_BLUE_ADDRESS, MORPHO_API_URL,
                              _fetch_borrower_positions, _num)
    from chain.simulate import (ORACLE_ABI, to_assets_up, max_borrow,
                                read_market_context, read_position, _checksum)

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit('RPC_URL not set. Export it from .env first:  '
                 'export RPC_URL="$(grep ^RPC_URL= .env | cut -d= -f2-)"')
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)

    # isAuthorized is not in the bot's read ABI -> minimal fragment (read-only).
    ISAUTH_ABI = [{"name": "isAuthorized", "type": "function", "stateMutability": "view",
                   "inputs": [{"name": "authorizer", "type": "address"},
                              {"name": "authorized", "type": "address"}],
                   "outputs": [{"name": "", "type": "bool"}]}]
    auth = rpc.contract(MORPHO_BLUE_ADDRESS, ISAUTH_ABI)

    print(f"[*] Morpho {MORPHO_BLUE_ADDRESS}  chain {cfg.chain_id}  (read-only, no tx)\n")
    for t in TARGETS:
        pair, mid, preliq = t["pair"], t["market_id"], _checksum(t["preliq"])
        pre_lltv_wad = t["pre_lltv_wad"]
        print(f"=== {pair}  preLiq {preliq}  preLltv {pre_lltv_wad / 10**18:.4f}")

        ctx = read_market_context(rpc, MORPHO_BLUE_ADDRESS, mid)   # 3 calls: params/state/mkt-oracle
        pre_price = rpc.contract(t["preliq_oracle"], ORACLE_ABI).functions.price().call()

        borrowers = _fetch_borrower_positions([mid], MORPHO_API_URL)
        meaningful = [b for b in borrowers
                      if _num((b.get("state") or {}).get("borrowAssetsUsd")) >= MIN_DEBT_USD]
        print(f"    borrowers (debt>=${MIN_DEBT_USD:.0f}): {len(meaningful)} / {len(borrowers)} total")

        opted, live, near = [], [], []
        for b in meaningful[:MAX_CHECK]:
            addr = ((b.get("user") or {}).get("address") or "")
            if not addr:
                continue
            ca = _checksum(addr)
            try:
                if not auth.functions.isAuthorized(ca, preliq).call():
                    continue
            except Exception:
                continue
            bs, col = read_position(rpc, MORPHO_BLUE_ADDRESS, mid, ca)
            borrowed = to_assets_up(bs, ctx.total_borrow_assets, ctx.total_borrow_shares)
            if borrowed <= 0 or col <= 0:
                continue
            hf_pre = max_borrow(col, pre_price, pre_lltv_wad) / borrowed
            hf_mkt = max_borrow(col, ctx.price, ctx.lltv_wad) / borrowed
            usd = _num((b.get("state") or {}).get("borrowAssetsUsd"))
            opted.append(ca)
            flag = ""
            if hf_pre < 1.0:
                live.append(ca); flag = "  <-- LIVE (pre-liquidatable now)"
            elif hf_pre <= 1.0 / NEAR_FRAC:
                near.append(ca); flag = "  <-- NEAR pre-liq threshold"
            print(f"      opted-in {ca}  preHF {hf_pre:.4f}  mktHF {hf_mkt:.4f}  debt ${usd:,.0f}{flag}")

        if not opted:
            verdict = "DEAD (0 borrowers opted in — contract deployed but unused)"
        elif live:
            verdict = (f"LIVE ({len(live)} opted-in at/above preLltv — "
                       "real pre-liq target on a familiar asset)")
        elif near:
            verdict = f"WATCH ({len(near)} opted-in near threshold; none liquidatable yet)"
        else:
            verdict = f"DORMANT ({len(opted)} opted in, all healthy below preLltv)"
        print(f"    VERDICT: {verdict}\n")

    print("[done] File the verdicts in STATE.md. All DEAD/DORMANT -> pre-liq on Base closed. "
          "Any LIVE -> backlog 'verified pre-liq tail (cbXRP/cbADA)', behind first submitted:1.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
