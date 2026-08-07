"""Деплой контракта v4 (authorized-set) на Base с Шарлотты — В ОСТАНОВЛЕННОМ ОКНЕ БОТА
(общий кошелёк = общий nonce-поток; при живом боте деплой рвал бы _LOCAL_NONCE).

Шаги (последовательные nonce, каждая tx ждёт receipt, base fee свежий на каждую):
  1. деплой creation-кода из /tmp/deploy_v4.hex (или реюз V4_ADDR из окружения),
     верификация owner()/MORPHO();
  2. ключи: реюз SPEC_FIRE_EOA_KEY_* из .env, иначе генерация и запись в .env
     НЕМЕДЛЕННО — до первой отправки ETH (падение после патрона не теряет ключи);
  3. патрон FUND_ETH каждому EOA (пропуск уже профинансированных);
  4. setAuthorized(eoa, True) на v4 (пропуск уже авторизованных) + чтение isAuthorized;
  5. LIQUIDATOR_ADDRESS в .env -> v4 (старый в комментарий) — ПОСЛЕДНИМ шагом.

Идемпотентен: любой сбой лечится повторным запуском (при задеплоенном v4 —
V4_ADDR=0x... venv/bin/python analysis/deploy_v4_charlotte.py). Бэкап .env поверх
живого НЕ раскатывать: после шага 2 там ключи профинансированных EOA.
Приватники НЕ печатаются никогда.
"""
import os
import sys

ENV_PATH = "/root/liquidator/.env"
HEX_PATH = "/tmp/deploy_v4.hex"
N_KEYS = 4
FUND_ETH = 0.03
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"


def load_env(path):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def main():
    # гейт: бот обязан быть остановлен (общий nonce-поток)
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            if os.readlink("/proc/%s/cwd" % p) == "/root/liquidator":
                with open("/proc/%s/cmdline" % p, "rb") as f:
                    if b"main.py" in f.read():
                        print("GATE-FAIL: бот жив (PID %s) — стоп перед деплоем" % p)
                        sys.exit(1)
        except OSError:
            continue

    env = load_env(ENV_PATH)
    from web3 import Web3
    from eth_account import Account
    w3 = Web3(Web3.HTTPProvider(env["RPC_URL"]))
    acct = Account.from_key(env["WALLET_KEY"])
    bot = acct.address
    nonce = w3.eth.get_transaction_count(bot, "pending")
    bal0 = w3.eth.get_balance(bot)
    print("wallet=%s balance=%.5f ETH start_nonce=%d" % (bot, bal0 / 1e18, nonce))

    tip = int(0.05e9)

    def send(tx, label):
        nonlocal nonce
        base = int(w3.eth.get_block("latest")["baseFeePerGas"])  # свежий на каждую tx
        max_fee = base * 2 + tip
        tx.update({"from": bot, "nonce": nonce, "chainId": 8453,
                   "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip})
        if "gas" not in tx:
            tx["gas"] = int(w3.eth.estimate_gas(tx) * 12 // 10)
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        assert rcpt["status"] == 1, "%s REVERTED tx=%s" % (label, h.hex())
        gas_eth = rcpt["gasUsed"] * rcpt.get("effectiveGasPrice", max_fee) / 1e18
        print("%s: ok tx=%s gas=%.6f ETH blk=%d" % (label, h.hex(), gas_eth, rcpt["blockNumber"]))
        nonce += 1
        return rcpt

    # 1. деплой v4 (реюз V4_ADDR при повторном запуске)
    v4 = os.environ.get("V4_ADDR", "")
    if v4:
        v4 = Web3.to_checksum_address(v4)
        print("реюз V4_ADDRESS=%s (без повторного деплоя)" % v4)
    else:
        code = open(HEX_PATH).read().strip()
        rcpt = send({"data": code}, "deploy v4")
        v4 = rcpt["contractAddress"]
    print("V4_ADDRESS=%s" % v4)
    assert w3.eth.get_code(v4), "нет кода по адресу v4"

    def call_addr(to, sig):
        out = w3.eth.call({"to": to, "data": Web3.keccak(text=sig)[:4].hex()})
        return Web3.to_checksum_address("0x" + out.hex()[-40:])

    assert call_addr(v4, "owner()") == bot, "owner() != wallet"
    assert call_addr(v4, "MORPHO()") == MORPHO, "MORPHO() != canonical"
    print("verify: owner==wallet, MORPHO==canonical OK")

    # 2. ключи: реюз из .env, иначе генерация + запись ДО патрона
    accts = []
    if "SPEC_FIRE_EOA_KEY_2" in env:
        for i in range(N_KEYS):
            k = env.get("SPEC_FIRE_EOA_KEY_%d" % (i + 2))
            assert k, "частичный набор SPEC_FIRE_EOA_KEY_* в .env — разобрать вручную"
            accts.append(Account.from_key(k))
        print("ключи: реюз %d существующих из .env" % N_KEYS)
    else:
        accts = [Account.create() for _ in range(N_KEYS)]
        lines = ["", "# каскадный тир («го» kelbic 07.08): залповые EOA — ключи заберёт владелец"]
        lines += ["SPEC_FIRE_EOA_KEY_%d=%s" % (i + 2, a.key.hex())
                  for i, a in enumerate(accts)]
        with open(ENV_PATH, "a") as f:
            f.write("\n".join(lines) + "\n")
        print("ключи: %d новых записаны в .env ДО отправки патрона" % N_KEYS)
    eoas = [a.address for a in accts]
    print("volley EOAs: %s" % ", ".join(eoas))

    # 3. патрон (пропуск уже профинансированных)
    for a in eoas:
        if w3.eth.get_balance(a) >= int(FUND_ETH * 1e18):
            print("fund %s: уже профинансирован, пропуск" % a[:10])
            continue
        send({"to": a, "value": int(FUND_ETH * 1e18), "gas": 21000}, "fund %s" % a[:10])

    # 4. авторизация на v4 (пропуск уже авторизованных)
    sel_set = Web3.keccak(text="setAuthorized(address,bool)")[:4].hex()
    sel_is = Web3.keccak(text="isAuthorized(address)")[:4].hex()

    def is_auth(a):
        out = w3.eth.call({"to": v4, "data": sel_is + a[2:].lower().rjust(64, "0")})
        return int(out.hex(), 16) == 1

    for a in eoas:
        if is_auth(a):
            print("authorize %s: уже авторизован, пропуск" % a[:10])
            continue
        data = sel_set + a[2:].lower().rjust(64, "0") + "1".rjust(64, "0")
        send({"to": v4, "data": data}, "authorize %s" % a[:10])
    assert all(is_auth(a) for a in eoas), "isAuthorized != true не у всех EOA"
    print("verify: isAuthorized == true для всех %d EOA" % len(eoas))

    # 5. .env: LIQUIDATOR_ADDRESS -> v4 (последним шагом; до него .env указывает на v3)
    cur = load_env(ENV_PATH).get("LIQUIDATOR_ADDRESS", "")
    if cur == v4:
        print("env: LIQUIDATOR_ADDRESS уже v4, пропуск")
    else:
        raw = open(ENV_PATH).read()
        raw = raw.replace(
            "LIQUIDATOR_ADDRESS=%s" % cur,
            "# v3 (откат): LIQUIDATOR_ADDRESS=%s\nLIQUIDATOR_ADDRESS=%s" % (cur, v4), 1)
        open(ENV_PATH, "w").write(raw)
        print("env: LIQUIDATOR_ADDRESS %s -> %s" % (cur, v4))
    bal1 = w3.eth.get_balance(bot)
    print("итог: balance=%.5f ETH (потрачено %.5f)" % (bal1 / 1e18, (bal0 - bal1) / 1e18))


if __name__ == "__main__":
    main()
