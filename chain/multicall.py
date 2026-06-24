"""Multicall3 batch reader (Base: 0xcA11...CA11, same address every chain). Collapses N
Morpho position() reads into ONE eth_call so the scanner stays well within RPC limits.

Calldata is built/parsed with eth_abi (stable across web3 v6/v7, unlike the contract
encodeABI/encode_abi rename). web3/eth_abi are imported lazily to keep `import main`
clean for the runtime-import check (see docs/WORKFLOW.md).
"""
from __future__ import annotations

from chain.rpc import BaseRpc

# Canonical Multicall3 — deterministic deploy, identical address on Base and elsewhere.
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

MULTICALL3_ABI = [
    {"name": "aggregate3", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "calls", "type": "tuple[]", "components": [
         {"name": "target", "type": "address"},
         {"name": "allowFailure", "type": "bool"},
         {"name": "callData", "type": "bytes"}]}],
     "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
         {"name": "success", "type": "bool"},
         {"name": "returnData", "type": "bytes"}]}]},
]

# Morpho: position(bytes32,address) -> (uint256 supplyShares, uint128 borrowShares, uint128 collateral)
_POSITION_SIG = "position(bytes32,address)"
_POSITION_OUT = ["uint256", "uint128", "uint128"]


def _position_selector() -> bytes:
    from eth_utils import keccak
    return keccak(text=_POSITION_SIG)[:4]


def encode_position_call(mid: bytes, borrower: str) -> bytes:
    """selector ++ abi(bytes32, address) — the calldata for Morpho.position(id, user)."""
    from eth_abi import encode
    from web3 import Web3
    return _position_selector() + encode(["bytes32", "address"], [mid, Web3.to_checksum_address(borrower)])


def decode_position(data: bytes):
    """Decode position() return -> (borrow_shares, collateral). supplyShares ignored."""
    from eth_abi import decode
    _supply, borrow_shares, collateral = decode(_POSITION_OUT, data)
    return borrow_shares, collateral


def _selector(sig: str) -> bytes:
    from eth_utils import keccak
    return keccak(text=sig)[:4]


# --- Morpho read encoders (calldata) / decoders, version-neutral via eth_abi ---

def encode_market_call(mid: bytes) -> bytes:
    from eth_abi import encode
    return _selector("market(bytes32)") + encode(["bytes32"], [mid])


def decode_market(data: bytes):
    """-> (totalSupplyAssets, totalSupplyShares, totalBorrowAssets, totalBorrowShares, lastUpdate, fee)"""
    from eth_abi import decode
    return decode(["uint128", "uint128", "uint128", "uint128", "uint128", "uint128"], data)


def encode_id_to_market_params_call(mid: bytes) -> bytes:
    from eth_abi import encode
    return _selector("idToMarketParams(bytes32)") + encode(["bytes32"], [mid])


def decode_id_to_market_params(data: bytes):
    """-> (loanToken, collateralToken, oracle, irm, lltv)"""
    from eth_abi import decode
    return decode(["address", "address", "address", "address", "uint256"], data)


def encode_price_call() -> bytes:
    return _selector("price()")   # no args, called on the oracle contract


def decode_price(data: bytes) -> int:
    from eth_abi import decode
    return decode(["uint256"], data)[0]


def aggregate3(rpc: BaseRpc, calls: list, chunk: int = 500, block_identifier=None) -> list:
    """Generic Multicall3: calls = [(target_addr, calldata_bytes)]. allowFailure=True.
    Returns [(success: bool, return_bytes: bytes)] aligned with `calls`. Chunked so each
    eth_call stays a reasonable size; a handful of chunks still cost ~chunks eth_calls.
    block_identifier='pending' reads against the pre-confirmed state (preconf RPC)."""
    from web3 import Web3
    mc = rpc.contract(MULTICALL3_ADDRESS, MULTICALL3_ABI)
    out = []
    for i in range(0, len(calls), chunk):
        agg = [(Web3.to_checksum_address(t), True, cd) for (t, cd) in calls[i:i + chunk]]
        fn = mc.functions.aggregate3(agg)
        rows = fn.call(block_identifier=block_identifier) if block_identifier else fn.call()
        for success, data in rows:
            out.append((bool(success), bytes(data)))
    return out


def batch_positions(rpc: BaseRpc, morpho_addr: str, market_id: str, borrowers: list):
    """ONE eth_call (aggregate3) for all borrowers of a market.
    Returns a list aligned with `borrowers`: (borrow_shares, collateral) or None on failure."""
    if not borrowers:
        return []
    from web3 import Web3
    mid = rpc.to_bytes32(market_id)
    target = Web3.to_checksum_address(morpho_addr)
    calls = [(target, True, encode_position_call(mid, b)) for b in borrowers]
    mc = rpc.contract(MULTICALL3_ADDRESS, MULTICALL3_ABI)
    results = mc.functions.aggregate3(calls).call()
    out = []
    for success, data in results:
        out.append(decode_position(data) if (success and data) else None)
    return out
