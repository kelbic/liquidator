"""Деплой контракта v5 (stateful board) на Base с Шарлотты — В ОСТАНОВЛЕННОМ ОКНЕ БОТА
(общий кошелёк = общий nonce-поток; при живом боте деплой рвал бы _LOCAL_NONCE).

v5 = v4 + armed-план: owner кладёт полный план ликвидации ДО окна (setPlan), залповые EOA
пробят <=1 байтом калдаты через fallback(). liquidate/batchLiquidate НЕ менялись, поэтому
переключение адреса v4->v5 при SPEC_FIRE_V2_BOARD=0 обязано быть поведенчески нейтральным —
именно так и разносим: сперва адрес (проверяемо), потом доска (отдельным решением).

Шаги (последовательные nonce, каждая tx ждёт receipt, base fee свежий на каждую):
  1. деплой creation-кода из /tmp/deploy_v5.hex (или реюз V5_ADDR), верификация
     owner()/MORPHO() И planInfo() — последнее доказывает, что задеплоен именно v5;
  2. ключи залповых EOA: ТОЛЬКО реюз из .env — v5 не генерирует и не патронит ничего
     (EOA уже профинансированы под v4; новые ключи здесь были бы чистым риском);
  3. setAuthorized(eoa, True) на v5 (пропуск уже авторизованных) + чтение isAuthorized;
  4. LIQUIDATOR_ADDRESS в .env -> v5, старый в комментарий — ПОСЛЕДНИМ шагом.

Идемпотентен: любой сбой лечится повторным запуском (V5_ADDR=0x... при задеплоенном).
Бэкап .env поверх живого НЕ раскатывать — там ключи профинансированных EOA.
SPEC_FIRE_V2_BOARD этот скрипт НЕ трогает: доска включается отдельно, после проверки
что v5 работает как v4. Приватники НЕ печатаются никогда.
"""
import os
import sys

ENV_PATH = "/root/liquidator/.env"
HEX_PATH = "/tmp/deploy_v5.hex"
N_KEYS = 4
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
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        assert rcpt["status"] == 1, "%s REVERTED tx=%s" % (label, h.hex())
        gas_eth = rcpt["gasUsed"] * rcpt.get("effectiveGasPrice", max_fee) / 1e18
        print("%s: ok tx=%s gas=%.6f ETH blk=%d" % (label, h.hex(), gas_eth, rcpt["blockNumber"]))
        nonce += 1
        return rcpt

    # 1. деплой v5
    v5 = os.environ.get("V5_ADDR", "")
    if v5:
        v5 = Web3.to_checksum_address(v5)
        print("реюз V5_ADDR=%s (без повторного деплоя)" % v5)
    else:
        code = open(HEX_PATH).read().strip()
        rcpt = send({"data": code}, "deploy v5")
        v5 = rcpt["contractAddress"]
    print("V5_ADDRESS=%s" % v5)
    assert w3.eth.get_code(v5), "нет кода по адресу v5"

    def call_addr(to, sig):
        out = w3.eth.call({"to": to, "data": Web3.keccak(text=sig)[:4].hex()})
        return Web3.to_checksum_address("0x" + out.hex()[-40:])

    assert call_addr(v5, "owner()") == bot, "owner() != wallet"
    assert call_addr(v5, "MORPHO()") == MORPHO, "MORPHO() != canonical"
    # ПОЗИТИВНЫЙ КОНТРОЛЬ версии: planInfo() есть только у v5. Без него «задеплоили v5»
    # держалось бы на честном слове — а v4 по этому же пути отвечает ревертом.
    pi = w3.eth.call({"to": v5, "data": Web3.keccak(text="planInfo()")[:4].hex()})
    assert len(pi) == 96, "planInfo() вернул %d байт — это НЕ v5" % len(pi)
    assert int(pi.hex()[64:128], 16) == 0, "свежий v5 обязан быть с пустым планом"
    print("verify: owner==wallet, MORPHO==canonical, planInfo() отвечает (это v5), план пуст")

    # 2. ключи: ТОЛЬКО реюз (v5 ничего не генерирует и не патронит)
    accts = []
    for i in range(N_KEYS):
        k = env.get("SPEC_FIRE_EOA_KEY_%d" % (i + 2))
        assert k, "нет SPEC_FIRE_EOA_KEY_%d в .env — v5 ключи НЕ генерирует" % (i + 2)
        accts.append(Account.from_key(k))
    eoas = [a.address for a in accts]
    print("volley EOAs (реюз): %s" % ", ".join(eoas))
    for a in eoas:
        print("  %s патрон %.4f ETH" % (a[:10], w3.eth.get_balance(a) / 1e18))

    # 3. авторизация на v5
    sel_set = Web3.keccak(text="setAuthorized(address,bool)")[:4].hex()
    sel_is = Web3.keccak(text="isAuthorized(address)")[:4].hex()

    def is_auth(a):
        out = w3.eth.call({"to": v5, "data": sel_is + a[2:].lower().rjust(64, "0")})
        return int(out.hex(), 16) == 1

    for a in eoas:
        if is_auth(a):
            print("authorize %s: уже авторизован, пропуск" % a[:10])
            continue
        data = sel_set + a[2:].lower().rjust(64, "0") + "1".rjust(64, "0")
        send({"to": v5, "data": data}, "authorize %s" % a[:10])
    assert all(is_auth(a) for a in eoas), "isAuthorized != true не у всех EOA"
    print("verify: isAuthorized == true для всех %d EOA" % len(eoas))

    # 4. .env: LIQUIDATOR_ADDRESS -> v5 (последним шагом)
    cur = load_env(ENV_PATH).get("LIQUIDATOR_ADDRESS", "")
    if cur.lower() == v5.lower():
        print("env: LIQUIDATOR_ADDRESS уже v5, пропуск")
    else:
        raw = open(ENV_PATH).read()
        assert ("LIQUIDATOR_ADDRESS=%s" % cur) in raw, "не нашёл строку LIQUIDATOR_ADDRESS"
        raw = raw.replace(
            "LIQUIDATOR_ADDRESS=%s" % cur,
            "# v4 (откат): LIQUIDATOR_ADDRESS=%s\nLIQUIDATOR_ADDRESS=%s" % (cur, v5), 1)
        open(ENV_PATH, "w").write(raw)
        print("env: LIQUIDATOR_ADDRESS %s -> %s" % (cur, v5))
    bal1 = w3.eth.get_balance(bot)
    print("итог: balance=%.5f ETH (потрачено %.5f)" % (bal1 / 1e18, (bal0 - bal1) / 1e18))
    print("ДАЛЬШЕ: доска (SPEC_FIRE_V2_BOARD=1) — ОТДЕЛЬНЫМ шагом, после проверки что v5 "
          "ведёт себя как v4")


if __name__ == "__main__":
    main()
