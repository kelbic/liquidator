"""Base RPC access. web3 is imported lazily so the module stays import-clean for
the runtime-import check (see docs/WORKFLOW.md). Phase 1 reads only."""
from __future__ import annotations


class BaseRpc:
    def __init__(self, rpc_url: str, chain_id: int):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self._w3 = None

    def _web3(self):
        if self._w3 is None:
            from web3 import Web3                 # lazy: keeps `import main` clean
            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        return self._w3

    def contract(self, address: str, abi: list):
        """Bound contract for read calls. web3 stays lazy (loaded on first use)."""
        w3 = self._web3()
        return w3.eth.contract(address=w3.to_checksum_address(address), abi=abi)

    def to_bytes32(self, hexstr: str):
        from web3 import Web3
        return Web3.to_bytes(hexstr=hexstr)

    def eth_call(self, tx: dict, block="latest", state_override=None):
        """Low-level eth_call. state_override (optional) injects {addr: {code, stateDiff}} —
        used to simulate the not-yet-deployed liquidator before a real deploy."""
        w3 = self._web3()
        if state_override is not None:
            return w3.eth.call(tx, block, state_override)
        return w3.eth.call(tx, block)

    def block_number(self) -> int:
        return self._web3().eth.block_number

    # TODO(phase1): subscribe to new blocks / oracle updates (wss),
    #   batch eth_call for health factors, read base fee for tip accounting.
