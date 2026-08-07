"""Канарейка каскадного тира (07.08, как канарейка 24.07): 0-value self-tx с ДВУХ залповых
EOA, подписи заранее, отправка встык — проверяем боевую механику тира без огня по целям:
ключи из .env валидны, nonce-потоки EOA независимы, патрон тратится, обе tx садятся
(в идеале одним блоком — разные отправители не толкаются в очереди).

Бота НЕ трогаем: залповые EOA вне nonce-потока главного кошелька.
Приватники не печатаются. Запуск: venv/bin/python analysis/volley_canary.py
"""
import time

ENV_PATH = "/root/liquidator/.env"


def main():
    env = {}
    for line in open(ENV_PATH):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    from web3 import Web3
    from eth_account import Account
    w3 = Web3(Web3.HTTPProvider(env["RPC_URL"]))
    accts = [Account.from_key(env[k]) for k in ("SPEC_FIRE_EOA_KEY_2", "SPEC_FIRE_EOA_KEY_3")]

    base = int(w3.eth.get_block("latest")["baseFeePerGas"])
    tip = int(0.05e9)
    raws = []
    for a in accts:
        bal = w3.eth.get_balance(a.address)
        nonce = w3.eth.get_transaction_count(a.address, "pending")
        print("EOA %s: balance=%.5f ETH nonce=%d" % (a.address, bal / 1e18, nonce))
        tx = {"to": a.address, "value": 0, "gas": 21000, "nonce": nonce, "chainId": 8453,
              "maxFeePerGas": base * 2 + tip, "maxPriorityFeePerGas": tip}
        signed = a.sign_transaction(tx)
        raws.append(getattr(signed, "raw_transaction", None) or signed.rawTransaction)

    t0 = time.perf_counter()
    hashes = [w3.eth.send_raw_transaction(r) for r in raws]     # встык, без ожиданий между
    dt_ms = (time.perf_counter() - t0) * 1000.0
    print("отправлены встык за %.0fмс: %s" % (dt_ms, ", ".join(h.hex() for h in hashes)))

    blocks = []
    for a, h in zip(accts, hashes):
        r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
        assert r["status"] == 1, "canary %s REVERTED" % a.address
        blocks.append(int(r["blockNumber"]))
        print("canary %s: blk=%d idx=%d gas=%.7f ETH" % (
            a.address[:10], r["blockNumber"], r["transactionIndex"],
            r["gasUsed"] * r.get("effectiveGasPrice", 0) / 1e18))
    print("VOLLEY_CANARY_OK same_block=%s (blk %s)" % (blocks[0] == blocks[1], blocks))


if __name__ == "__main__":
    main()
