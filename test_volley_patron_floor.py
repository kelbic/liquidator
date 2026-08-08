"""Пол патрона на пути движка: незнание ЗАКРЫВАЕТ гард, расход списывается по КВИТАНЦИИ.

Ревью 08.08 (blocker): `bal = _VOLLEY_BAL.get(addr); if bal is not None:` был сломан в обе
стороны. (1) Пустой кэш = гейт молча пропускался — нога стреляла вслепую (тот же класс, что
дал первые три боевых выстрела katana в минус: «X if px else <мягко>»). (2) Кэш на этом пути
НИКОГДА не обновлялся, а декремент по ЭСТИМЕЙТУ (45k x maxFee) монотонно гнал его к полу:
нога самоглушилась за ~135-155 проб при реальном расходе no-op 0.00000009 ETH — завышение
в ~250 раз. Соседняя V1-нога (send_volley_probe) делала правильно и то, и другое.

Решение владельца: декремент по ФАКТИЧЕСКОЙ квитанции, пустой кэш — пропуск с АСИНХРОННЫМ
дозором (синхронный get_balance на пути огня = RTT в горячем окне), отказы — счётчиком в
экспорт. Исполняется НАСТОЯЩИЙ send_presigned_probe, шов — только транспорт и get_balance.
"""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                               # noqa: E402

ADDR = "0x" + "c" * 40
SEQ = ["http://sequencer.test"]
GAS = 3_000_000                                          # SPEC_FIRE_V2_FIRE_GAS (лимит выигрыша)
MAXFEE = 5 * 10**8                                       # 0.5 gwei
FUNDED = int(0.008 * 10**18)                             # заправка ноги по факту флота


class _Rpc:
    """Шов на чтении баланса: считаем ОБРАЩЕНИЯ — дозор обязан быть один на ногу."""

    def __init__(self, bal_wei=FUNDED):
        self.reads = []
        self._bal = bal_wei
        self._w3 = self

    class _Eth:
        pass

    def _web3(self):
        outer = self

        class _W3:
            class eth:
                @staticmethod
                def get_balance(a):
                    outer.reads.append(a)
                    time.sleep(0.05)                     # RTT: доказывает, что дозор НЕ в огне
                    return outer._bal
        return _W3


def seed_ladder(n=400):
    ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
    ex._VOLLEY_LADDER[ADDR] = {"q": [(i, "0xdead", "0xh%d" % i) for i in range(n)],
                               "gas": GAS, "max_fee": MAXFEE}
    ex._VOLLEY_NONCE.pop(ADDR, None)


_saved = (ex._post_raw_sync, ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY,
          ex._VOLLEY_FLOOR_SKIPS, ex._VOLLEY_UNKNOWN_SKIPS)
try:
    ex._VOLLEY_DAY = int(time.time() // 86400)
    ex._VOLLEY_GAS_USD = 0.0
    ex._INFLIGHT = 0
    ex._post_raw_sync = lambda url, raw, timeout=5: (True, None)

    # --- 1. ПУСТОЙ КЭШ: не стреляем вслепую, будим дозор, отказ виден счётчиком ------------
    seed_ladder()
    ex._VOLLEY_BAL.pop(ADDR, None)
    ex._VOLLEY_FLOOR_SKIPS = ex._VOLLEY_UNKNOWN_SKIPS = 0
    rpc = _Rpc()
    t0 = time.monotonic()
    assert ex.send_presigned_probe(rpc, None, ADDR, broadcast_raw_urls=SEQ) is None, \
        "пустой кэш патрона ПРОПУСТИЛ пробу — незнание снова как разрешение"
    assert time.monotonic() - t0 < 0.04, "дозор выполнен СИНХРОННО: RTT попал в путь огня"
    assert len(ex._VOLLEY_LADDER[ADDR]["q"]) == 400, "гейт закрыт, а запись потрачена"
    assert ex.volley_probe_skips()[1] == 1, ex.volley_probe_skips()

    # дозор долетает и засевает кэш -> следующая проба УХОДИТ (fail-closed самолечится)
    for _ in range(60):
        if ADDR in ex._VOLLEY_BAL:
            break
        time.sleep(0.02)
    assert ex._VOLLEY_BAL.get(ADDR), "дозор не засеял кэш — нога осталась немой навсегда"
    assert ex.send_presigned_probe(rpc, None, ADDR, broadcast_raw_urls=SEQ) == "0xh0", \
        "кэш засеян дозором, а нога всё ещё молчит"

    # --- 2. РЕГРЕССИЯ ГЛАВНОГО ДЕФЕКТА: нога не глохнет на ~135-й пробе -------------------
    # Старый декремент (45k x maxFee за пробу) съедал зазор до пола за ~155 проб.
    seed_ladder()
    ex._VOLLEY_BAL[ADDR] = [FUNDED, time.monotonic()]
    fired = 0
    for _ in range(300):
        if ex.send_presigned_probe(rpc, None, ADDR, broadcast_raw_urls=SEQ) is not None:
            fired += 1
    assert fired == 300, "нога самоглушится: выстрелов %d из 300 (старый дефект жив)" % fired

    # --- 3. КОНТРОЛЬ: пол ВООБЩЕ работает — иначе п.2 неотличим от «гейта нет» ------------
    seed_ladder()
    ex._VOLLEY_FLOOR_SKIPS = 0
    ex._VOLLEY_BAL[ADDR] = [ex._VOLLEY_BAL_FLOOR_WEI + GAS * MAXFEE - 1, time.monotonic()]
    assert ex.send_presigned_probe(rpc, None, ADDR, broadcast_raw_urls=SEQ) is None, \
        "патрон ниже пола, а нога стреляет"
    assert len(ex._VOLLEY_LADDER[ADDR]["q"]) == 400
    assert ex.volley_probe_skips()[0] == 1, "отказ пола не попал в экспорт"

    # --- 4. СПИСАНИЕ ПО ФАКТУ КВИТАНЦИИ ---------------------------------------------------
    ex._VOLLEY_BAL[ADDR] = [FUNDED, time.monotonic()]
    ex.volley_bal_charge(ADDR, 18600 * MAXFEE)           # реальный no-op доски
    assert ex._VOLLEY_BAL[ADDR][0] == FUNDED - 18600 * MAXFEE, ex._VOLLEY_BAL[ADDR][0]
    ex.volley_bal_charge(ADDR, 0)                        # нулевая/битая квитанция не двигает
    assert ex._VOLLEY_BAL[ADDR][0] == FUNDED - 18600 * MAXFEE
    ex.volley_bal_charge("0x" + "9" * 40, 10**18)        # чужая нога без кэша — не падаем
    ex._VOLLEY_BAL.pop(ADDR, None)
    ex.volley_bal_charge(ADDR, 10**15)                   # без кэша списывать нечего
    assert ADDR not in ex._VOLLEY_BAL

    # --- 5. ПРОТУХШИЙ СВЕРХ ПРЕДЕЛА = НЕИЗВЕСТНЫЙ (не живём вечно на древнем числе) -------
    seed_ladder()
    ex._VOLLEY_UNKNOWN_SKIPS = 0
    ex._VOLLEY_BAL[ADDR] = [FUNDED, time.monotonic() - ex._VOLLEY_BAL_MAX_AGE - 1]
    assert ex.send_presigned_probe(rpc, None, ADDR, broadcast_raw_urls=SEQ) is None
    assert ex.volley_probe_skips()[1] == 1

    # 5б. КОНТРОЛЬ: протухший по TTL, но моложе предела — СТРЕЛЯЕМ (молчать весь RTT в
    #     горячем окне дороже расхода одной пробы), дозор идёт фоном
    seed_ladder()
    ex._VOLLEY_BAL[ADDR] = [FUNDED, time.monotonic() - ex._VOLLEY_BAL_TTL - 1]
    rpc2 = _Rpc()
    assert ex.send_presigned_probe(rpc2, None, ADDR, broadcast_raw_urls=SEQ) == "0xh0", \
        "кэш всего лишь протух по TTL — нога обязана стрелять, а дозор идти фоном"

    # --- 6. ДОЗОР ОДИН НА НОГУ: 8 тредов движка не должны дать шторм get_balance ----------
    ex._VOLLEY_BAL.pop(ADDR, None)
    rpc3 = _Rpc()
    ths = [threading.Thread(target=lambda: ex.send_presigned_probe(
        rpc3, None, ADDR, broadcast_raw_urls=SEQ)) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    time.sleep(0.15)
    assert len(rpc3.reads) <= 2, "дозор устроил шторм: %d чтений баланса" % len(rpc3.reads)
finally:
    (ex._post_raw_sync, ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY,
     ex._VOLLEY_FLOOR_SKIPS, ex._VOLLEY_UNKNOWN_SKIPS) = _saved
    for d in (ex._VOLLEY_LOCKS, ex._VOLLEY_LADDER, ex._VOLLEY_BAL, ex._VOLLEY_NONCE):
        d.pop(ADDR, None)

# --- 7. СТРУКТУРНЫЙ КОНТРОЛЬ: эстимейт-декремент не вернулся на путь огня ----------------
src = pathlib.Path("chain/execute.py").read_text()
_fn = src[src.index("def send_presigned_probe"):src.index("def _post_raw_sync")]
assert "_VOLLEY_EXPECTED_GAS" not in _fn, \
    "декремент по эстимейту вернулся в send_presigned_probe — нога снова заглохнет за ~135 проб"
assert "volley_bal_charge" in pathlib.Path("chain/spec_fire_live.py").read_text(), \
    "коллектор квитанций движка не списывает патрон по факту"

print("OK: незнание закрывает гард патрона, расход списывается по квитанции, отказы видны")
