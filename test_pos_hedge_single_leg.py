"""#117: read_positions стартовал ОБЕ ноги хеджа безусловно — 3.5M CU/мес выброшенной страховки.

Ценовой хедж `read_price_hedged` этот класс расхода закрыл: «пока preconf здоров — одна нога,
мы больше не платим за неиспользуемую страховку». `read_positions` ту же правку НЕ получил и
запускал confirmed-ногу на БОЕВОМ ключе на КАЖДОЕ чтение, хотя её результат при живом preconf
выбрасывается ветвью `res = _rp`.

Цена замерена по журналу: 4 428 чтений/сут (ровно число hotset-строк, потому что _dump_hotset
зовётся раз на read_positions) = 115K CU/сут ≈ 3.5M CU/мес. Что страховка выбрасывается почти
всегда — видно независимо от нашей телеметрии: резервный ключ B, живущий на том же признаке
px_hedge_degraded, за сутки дал НОЛЬ обращений.

Это ДЕНЕЖНЫЙ реакционный путь, поэтому стенд гоняет НАСТОЯЩИЙ read_positions (подменён только
транспорт aggregate3) и проверяет три вещи, каждую как ФАКТ:
  * preconf здоров -> ОДНА нога (боевой не тронут) И результат БИТ-В-БИТ тот же, что при двух;
  * preconf деградировал -> ДВЕ ноги параллельно, как было (латентность страховки сохранена);
  * POS_HEDGE_ALWAYS_BOTH=1 -> прежнее поведение бит-в-бит при любом здоровье preconf;
  * одноногий промах preconf -> confirmed всё равно обслуживает чтение (жертва не теряется);
  * обе ноги мертвы -> None, как раньше (вызыватель считает f_norp и идёт к соседям).

Красный-на-базе: на HEAD confirmed-нога стартует всегда, поэтому «одна нога при здоровом
preconf» проваливается, а POS_HEDGE_ALWAYS_BOTH не существует.
"""
import sys
import threading
import time
import types

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.hotpath as hp        # noqa: E402
import chain.multicall as mc      # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


PRECONF = types.SimpleNamespace(rpc_url="https://preconf.example")
COMBAT = types.SimpleNamespace(rpc_url="https://combat.example/AKEY")
MID = "0x" + "ab" * 32
BORROWERS = ["0x" + "11" * 20, "0x" + "22" * 20]

# Ответ multicall: market() + position() на каждого заёмщика.
GOOD = [(True, b"MK")] + [(True, b"P%d" % i) for i in range(len(BORROWERS))]

CALLS = []          # (rpc_url, block_identifier) каждой РЕАЛЬНО улетевшей ноги
CALLS_LOCK = threading.Lock()


def _fake_agg(rpc, calls, block_identifier=None):
    with CALLS_LOCK:
        CALLS.append((getattr(rpc, "rpc_url", "?"), block_identifier))
    mode = MODE.get(getattr(rpc, "rpc_url", ""), "ok")
    if mode == "slow":
        time.sleep(1.2)                       # заведомо дольше _POS_HEDGE_SEC
    if mode == "fail":
        raise RuntimeError("429")
    if mode == "empty":
        return [(False, b"")] + [(True, b"P0")]
    return GOOD


MODE = {}

_saved = {k: getattr(mc, k) for k in ("aggregate3", "decode_market", "decode_position")}
mc.aggregate3 = _fake_agg
mc.decode_market = lambda d: (0, 0, 4242, 777)
mc.decode_position = lambda d: (int(d[1:] or 0) + 5, int(d[1:] or 0) + 9)
# to_bytes32 живёт на rpc; у наших двойников его нет — подставим на оба.
PRECONF.to_bytes32 = COMBAT.to_bytes32 = lambda h: bytes(32)
# _dump_hotset пишет на диск — не в этом стенде.
_saved_dump = hp._dump_hotset
hp._dump_hotset = lambda *a, **k: None


def run(degraded=False, always_both=False, preconf_mode="ok", combat_mode="ok"):
    CALLS.clear()
    MODE.clear()
    MODE[PRECONF.rpc_url] = preconf_mode
    MODE[COMBAT.rpc_url] = combat_mode
    hp._PX_HEDGE_DEGRADED_UNTIL = (time.monotonic() + 60.0) if degraded else 0.0
    # getattr, а не прямой доступ: на БАЗЕ константы нет, и прямой доступ дал бы AttributeError —
    # то есть красный «модуль не тот», а не красный «поведение другое». Нам нужен второй:
    # на базе стенд обязан дойти до проверок и провалить именно «одна нога».
    sv = getattr(hp, "POS_HEDGE_ALWAYS_BOTH", False)
    hp.POS_HEDGE_ALWAYS_BOTH = always_both
    src = {}
    try:
        out = hp.read_positions(COMBAT, MID, BORROWERS, preconf_rpc=PRECONF, src_out=src)
    finally:
        hp.POS_HEDGE_ALWAYS_BOTH = sv
        hp._PX_HEDGE_DEGRADED_UNTIL = 0.0
    time.sleep(0.05)                          # дать доспавненным ногам отметиться, если они есть
    with CALLS_LOCK:
        seen = list(CALLS)
    return out, src.get("src"), seen


try:
    # ---- 1. Здоровый preconf -> ОДНА нога ----
    out_one, src_one, seen = run(degraded=False)
    check("здоровый preconf: боевой ключ НЕ тронут (одна нога)",
          all(u != COMBAT.rpc_url for u, _ in seen), seen)
    check("здоровый preconf: чтение обслужено preconf'ом", src_one == "preconf", src_one)
    check("здоровый preconf: ровно ОДИН запрос", len(seen) == 1, seen)

    # ---- 2. Результат БИТ-В-БИТ совпадает с прежним двуногим режимом ----
    out_two, src_two, seen2 = run(degraded=False, always_both=True)
    check("прежний режим (обе ноги): боевой ключ ТРОНУТ",
          any(u == COMBAT.rpc_url for u, _ in seen2), seen2)
    check("результат одноногого == результат двуногого (БИТ-В-БИТ)",
          out_one == out_two and out_one is not None, (out_one, out_two))
    check("источник в обоих режимах — preconf (семантика не поехала)",
          src_one == src_two == "preconf", (src_one, src_two))

    # ---- 3. Деградация -> ДВЕ ноги ПАРАЛЛЕЛЬНО (латентность страховки сохранена) ----
    out_d, src_d, seen_d = run(degraded=True)
    check("деградация preconf: обе ноги стартуют", len(seen_d) == 2, seen_d)
    check("деградация preconf: одна из них — боевой ключ",
          any(u == COMBAT.rpc_url for u, _ in seen_d), seen_d)
    check("деградация: preconf читается на 'pending', боевой на latest(None)",
          sorted((u == COMBAT.rpc_url, b) for u, b in seen_d)
          == sorted([(False, "pending"), (True, None)]), seen_d)
    check("деградация: результат прежний", out_d == out_one, (out_d, out_one))

    # ---- 4. Откат ручкой при деградации — тоже прежнее поведение ----
    out_ab, _, seen_ab = run(degraded=True, always_both=True)
    check("POS_HEDGE_ALWAYS_BOTH=1 + деградация -> две ноги, результат прежний",
          len(seen_ab) == 2 and out_ab == out_one, (seen_ab, out_ab))

    # ---- 5. Одноногий ПРОМАХ: жертва не теряется, страховка доспавнивается ----
    out_m, src_m, seen_m = run(degraded=False, preconf_mode="fail")
    check("одноногий промах preconf: confirmed доспавнен и обслужил чтение",
          out_m == out_one and src_m == "confirmed", (out_m, src_m))
    check("одноногий промах: боевой ключ задействован ТОЛЬКО теперь",
          any(u == COMBAT.rpc_url for u, _ in seen_m) and len(seen_m) == 2, seen_m)

    # Медленный preconf (таймаут, не исключение) — тот же исход.
    out_s, src_s, seen_s = run(degraded=False, preconf_mode="slow")
    check("одноногий таймаут preconf: confirmed доспавнен, чтение обслужено",
          out_s == out_one and src_s == "confirmed", (out_s, src_s))

    # Пустой/невалидный ответ preconf — тоже промах.
    out_e, src_e, _ = run(degraded=False, preconf_mode="empty")
    check("одноногий невалидный ответ preconf: уходим на confirmed",
          out_e == out_one and src_e == "confirmed", (out_e, src_e))

    # ---- 6. Обе ноги мертвы -> None (как раньше: вызыватель считает f_norp) ----
    out_dead, _, _ = run(degraded=False, preconf_mode="fail", combat_mode="fail")
    check("обе ноги мертвы -> None (прежний контракт с вызывателем)", out_dead is None, out_dead)
    out_dead2, _, _ = run(degraded=True, preconf_mode="fail", combat_mode="fail")
    check("обе ноги мертвы в деградации -> None", out_dead2 is None, out_dead2)

    # ---- 7. Без preconf_rpc путь не задет вовсе ----
    CALLS.clear(); MODE.clear()
    MODE[COMBAT.rpc_url] = "ok"
    out_np = hp.read_positions(COMBAT, MID, BORROWERS, preconf_rpc=None)
    check("preconf_rpc=None -> прежний confirmed-only путь, одно чтение",
          out_np == out_one and len(CALLS) == 1, (out_np, CALLS))
finally:
    for k, v in _saved.items():
        setattr(mc, k, v)
    hp._dump_hotset = _saved_dump
    hp._PX_HEDGE_DEGRADED_UNTIL = 0.0

print()
if FAILS:
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("гейт второй ноги read_positions: все проверки прошли")
