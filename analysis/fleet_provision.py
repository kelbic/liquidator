"""Генерация и заправка ФЛОТА залповых EOA — В ОСТАНОВЛЕННОМ ОКНЕ БОТА
(общий кошелёк = общий nonce-поток; при живом боте заправка рвала бы _LOCAL_NONCE).

Зачем. Замер 07.08 (блок 49642833): у конкурента 2592 tx от 2592 РАЗНЫХ EOA в ОДНОМ блоке —
2590 холостых зондов ровно по 27260 газа плюс один исполнитель на 709k с логами. Вся стена
стоила ~0.0036 ETH. Плотность упирается не в деньги и не в код, а в ЧИСЛО ключей: у нас 4.

Насыщение. При окне победы W=0.5-2.2мс лишними становятся пробы плотнее ~1/W ≈ 900-1480 в блок
(консервативный W=2.2мс / медианный 1.35мс). Каждый ствол даёт ~13 проб/блок (каденс 0.15с при
блоке 2с), значит потолок полезности ~70-111 стволов (главный кошелёк + EOA), а не 2592.
Больше ставить незачем: сверх насыщения платим за то же покрытие.

Идемпотентен и ПРЕРЫВАЕМ БЕЗОПАСНО: ключи пишутся в keyfile ДО первой отправки ETH (падение
после перевода не теряет ключи — иначе ETH ушёл бы на адрес, приватки которого мы не знаем).
Уже заправленные пропускаются, так что повторный запуск просто дозаправляет.

  N=106 FUND_ETH=0.008 venv/bin/python analysis/fleet_provision.py
  DRY=1 ... — только смета, без транзакций

Приватники НЕ печатаются никогда. keyfile создаётся 0600 и живёт ТОЛЬКО на боевой машине.
"""
import os
import sys

ENV_PATH = "/root/liquidator/.env"
KEYFILE = os.environ.get("KEYFILE", "/root/liquidator/.fleet_keys")
N = int(os.environ.get("N", "106"))
FUND_ETH = float(os.environ.get("FUND_ETH", "0.008"))
DRY = os.environ.get("DRY", "").lower() in ("1", "true", "yes")
# сколько ОСТАВИТЬ на главном кошельке при любом раскладе: патронный резерв под сам выстрел
# (GAS_BULLET_RESERVE_ETH) плюс запас на управляющие tx доски. Флот НЕ имеет права его съесть.
KEEP_ETH = float(os.environ.get("KEEP_ETH", "0.15"))


def load_env(path):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def main():
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            if os.readlink("/proc/%s/cwd" % p) == "/root/liquidator":
                with open("/proc/%s/cmdline" % p, "rb") as f:
                    if b"main.py" in f.read():
                        print("GATE-FAIL: бот жив (PID %s) — стоп перед заправкой" % p)
                        sys.exit(1)
        except OSError:
            continue

    env = load_env(ENV_PATH)
    # заправка НИЖЕ пола патрона = ноги, которые никогда не выстрелят (реестр их молча
    # пропустит) — молчаливо мёртвый флот; дефолт 0.0015 при поле 0.003 был ровно этим
    floor = float(env.get("SPEC_FIRE_VOLLEY_FLOOR_ETH",
                          os.environ.get("SPEC_FIRE_VOLLEY_FLOOR_ETH", "0.003")))
    if FUND_ETH <= floor:
        print("СТОП: FUND_ETH=%.4f <= пола патрона %.4f (SPEC_FIRE_VOLLEY_FLOOR_ETH) — "
              "такие ноги никогда не выстрелят. Подними FUND_ETH." % (FUND_ETH, floor))
        sys.exit(3)
    from web3 import Web3
    from eth_account import Account
    # 22.08: узел переопределяем (PROVISION_RPC_URL) — боевой Alchemy на 220 одиночных
    # балансов + 213 отправок ответил 429 посреди заправки. И ретраим 429/сеть с паузой:
    # упасть с половиной розданных ног хуже, чем подождать секунды (идемпотентность спасает,
    # но каждый рестарт скрипта — новый полный скан балансов).
    w3 = Web3(Web3.HTTPProvider(env.get("PROVISION_RPC_URL") or env["RPC_URL"]))
    import time as _t

    def _retry(fn, *a, **kw):
        for i in range(5):
            try:
                return fn(*a, **kw)
            except Exception:                            # noqa: BLE001
                if i == 4:
                    raise
                _t.sleep(2.0 * (i + 1))

    def _bal(a):
        return _retry(w3.eth.get_balance, a)
    acct = Account.from_key(env["WALLET_KEY"])
    bot = acct.address
    bal0 = w3.eth.get_balance(bot)
    print("главный кошелёк=%s баланс=%.5f ETH" % (bot, bal0 / 1e18))

    # 1. ключи: реюз существующего keyfile + дозагенерить до N. ЗАПИСЬ ДО ЛЮБОЙ ОТПРАВКИ.
    keys = []
    if os.path.exists(KEYFILE):
        mode = os.stat(KEYFILE).st_mode & 0o077
        assert not mode, "keyfile %s имеет права %o — поправь на 0600" % (KEYFILE, mode)
        keys = [l.strip() for l in open(KEYFILE) if l.strip() and not l.startswith("#")]
        print("keyfile: реюз %d ключей" % len(keys))
    need = max(0, N - len(keys))
    if need:
        if DRY:
            print("DRY: сгенерировал бы %d новых ключей" % need)
        else:
            keys += [Account.create().key.hex() for _ in range(need)]
            fd = os.open(KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(keys) + "\n")
            os.chmod(KEYFILE, 0o600)
            print("keyfile: +%d новых, всего %d (записаны ДО отправки ETH)" % (need, len(keys)))
    addrs = [Account.from_key(k).address for k in keys[:N]]

    # 2. смета: флот не имеет права съесть патронный резерв главного кошелька
    # TOPUP=1 (22.08, решение kelbic «долить ДО 0.006»): шлём ДЕФИЦИТ до уровня FUND_ETH,
    # а не полный FUND_ETH сверху (старый режим на p50=0.004 слал бы 0.006 => ноги по 0.010
    # и 1.28 ETH сметы). Пыль < MIN_TOPUP не гоняем — 21000 газа ради копейки.
    fund_wei = int(FUND_ETH * 1e18)
    topup = os.environ.get("TOPUP", "").lower() in ("1", "true", "yes")
    min_topup_wei = int(float(os.environ.get("MIN_TOPUP_ETH", "0.0005")) * 1e18)
    if topup:
        todo = []
        for a in addrs:
            d = fund_wei - _bal(a)
            if d >= min_topup_wei:
                todo.append((a, d))
        need_wei = sum(d for _, d in todo) + len(todo) * 21000 * int(0.06e9)
        print("к ДОзаправке (TOPUP до %.4f): %d из %d ног, дефицит => %.5f ETH" % (
            FUND_ETH, len(todo), len(addrs), need_wei / 1e18))
    else:
        todo = [(a, fund_wei) for a in addrs if _bal(a) < fund_wei]
        need_wei = len(todo) * (fund_wei + 21000 * int(0.06e9))
        print("к заправке: %d из %d ног по %.4f ETH => %.5f ETH" % (
            len(todo), len(addrs), FUND_ETH, need_wei / 1e18))
    print("останется на главном: %.5f ETH (порог KEEP=%.3f)" % (
        (bal0 - need_wei) / 1e18, KEEP_ETH))
    if bal0 - need_wei < KEEP_ETH * 1e18:
        print("СТОП: заправка съела бы патронный резерв. Уменьши N или FUND_ETH, "
              "либо пополни главный кошелёк.")
        sys.exit(2)
    if DRY:
        print("DRY: транзакций не отправлено")
        return

    # 3. заправка
    nonce = w3.eth.get_transaction_count(bot, "pending")
    tip = int(0.05e9)
    sent = 0
    base = int(_retry(w3.eth.get_block, "latest")["baseFeePerGas"])
    for a, val in todo:
        tx = {"from": bot, "to": a, "value": val, "gas": 21000, "nonce": nonce,
              "chainId": 8453, "maxFeePerGas": base * 4 + tip, "maxPriorityFeePerGas": tip}
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = signed.hash
        for _i in range(5):
            try:
                w3.eth.send_raw_transaction(raw)
                break
            except Exception as _e:                      # noqa: BLE001
                _s = str(_e)
                # повтор ПОСЛЕ принятого узлом = успех, а не отказ
                if "already known" in _s or "nonce too low" in _s or "ALREADY" in _s:
                    break
                if _i == 4:
                    raise
                _t.sleep(2.0 * (_i + 1))
        nonce += 1
        sent += 1
        if sent % 10 == 0 or sent == len(todo):
            print("  заправлено %d/%d (последняя tx=%s)" % (sent, len(todo), h.hex()))
    # ждём только ПОСЛЕДНЮЮ: nonce последовательны, значит её приём подтверждает все прежние
    w3.eth.wait_for_transaction_receipt(h, timeout=300)

    # 4. приёмка: считаем по ЦЕПИ, а не по числу отправленных tx (в TOPUP пыль — не недодача)
    _target = fund_wei - (min_topup_wei if topup else 0)
    ok = sum(1 for a in addrs if _bal(a) >= _target)
    bal1 = w3.eth.get_balance(bot)
    print("ИТОГ: заправлено по цепи %d/%d ног, потрачено %.5f ETH, на главном %.5f ETH" % (
        ok, len(addrs), (bal0 - bal1) / 1e18, bal1 / 1e18))
    if ok < len(addrs):
        print("часть ног недозаправлена — повтори запуск (идемпотентно)")
    print("ДАЛЬШЕ: keyfile (%s) в .env НЕ включать, пока нет pre-sign проб и гейта "
          "«залп в полёте»:" % KEYFILE)
    print("  каждый тик (0.15с) порождает залп в 1+8 тредах, фазовый охват 111 стволов ~5.5с")
    print("  => десятки залпов в полёте разом, сотни тредов на 2 ядрах (observer effect).")
    print("  Деньги паркуются уже сейчас; включение — после pre-sign пайплайна.")


if __name__ == "__main__":
    main()
