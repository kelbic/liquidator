"""П.2 (22.08): двух-бакетные патроны — тир A «win-loaded» (право на победу) и B «density»
(право на плотность). Приёмка по спеке: реплей maxFee эпизода 05:11 — старая формула глушит
ВЕСЬ флот (220/220 при патроне эпизода 0.004), новая: A-ноги (0.010) живы все, B режется
только admission'ом эскалационного окна; спокойный класс — молчат ноль в обоих тирах;
откат SPEC_FIRE_TIER_A_LEGS=0 — однотирное наследие бит-в-бит.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.execute as ex  # noqa: E402

_sv_legs = ex.SPEC_FIRE_TIER_A_LEGS
_sv_set = set(ex._TIER_A_SET)
_sv_ttp = ex.transit_tip_portfolio


def _reset(a_legs=40):
    ex.SPEC_FIRE_TIER_A_LEGS = a_legs
    ex._TIER_A_SET.clear()
    for k in ex._TIER_STATS:
        ex._TIER_STATS[k] = 0


A_LEGS = ["0xA%03d" % i for i in range(40)]
B_LEGS = ["0xB%03d" % i for i in range(180)]
ETH = 10**18

# ---- Реплей maxFee эпизода 05:11: basefee 20.6 мгвей x MULT 20 + tip 2568 мгвей ----
ESC_FEE = int(20_617_048 * 20 + 2_568_598_392)          # ~2.98 гвей
P90_FEE = int(5e9)                                      # p90 эскалации из спеки
CALM_FEE = int(20_617_048 * 20 + 1_200_000)             # спокойный класс ~0.41 гвей

# 1. СТАРАЯ формула (откат, TIER_A_LEGS=0): весь флот 0.004 нем — воспроизводим 05:11
_reset(a_legs=0)
mute_old = sum(1 for a in A_LEGS + B_LEGS
               if ex.volley_tier_gate_short(a, int(0.004 * ETH), 1_000_000, ESC_FEE))
check("реплей 05:11 старой формулой: mute 220/220 (как в бою)", mute_old == 220, mute_old)
check("откат не трогает тир-счётчики", ex._TIER_STATS["a_mute"] == 0
      and ex._TIER_STATS["b_mute"] == 0, ex._TIER_STATS)

# 2. НОВАЯ формула: A (0.010) живы все — и на факте эпизода, и на p90 5 гвей
_reset(a_legs=40)
ex._TIER_A_SET.update(A_LEGS)
mute_a = sum(1 for a in A_LEGS
             if ex.volley_tier_gate_short(a, int(0.010 * ETH), 1_000_000, ESC_FEE)
             or ex.volley_tier_gate_short(a, int(0.010 * ETH), 1_000_000, P90_FEE))
check("тир A: mute = 0 из 40 в эскалацию (факт 2.98 гвей И p90 5 гвей)", mute_a == 0, mute_a)
mute_b_esc = sum(1 for a in B_LEGS
                 if ex.volley_tier_gate_short(a, int(0.006 * ETH), 1_000_000, P90_FEE))
check("тир B в p90-эскалацию: mute ожидаем (инвариант #32 «оплати свой выигрыш» сохранён — "
      "B туда и не ходит, кап tip + admission)", mute_b_esc == 180, mute_b_esc)
check("тир B на ФАКТЕ эпизода (2.98 гвей): после долива 0.006 проходит с запасом 2e-5 — "
      "граница названа (следующий более резкий эпизод снова замкнёт B, и это штатно)",
      ex.volley_tier_gate_short("0xB000", int(0.006 * ETH), 1_000_000, ESC_FEE) is False)
mute_b_capped = sum(1 for a in B_LEGS
                    if ex.volley_tier_gate_short(a, int(0.006 * ETH), 1_000_000, CALM_FEE))
check("тир B на СВОЁМ (капнутом) maxFee: mute = 0 — немота не поголовная",
      mute_b_capped == 0, mute_b_capped)
check("A-нога с патроном ниже права на победу — mute с атрибуцией a_mute",
      ex.volley_tier_gate_short("0xA000", int(0.004 * ETH), 1_000_000, P90_FEE) is True
      and ex._TIER_STATS["a_mute"] == 1, ex._TIER_STATS)

# 2б. Линза (2): пол ПОВЕРХ права на победу — A с балансом «ровно на один выигрыш» молчит
check("A с патроном ровно на выигрыш (0.0065 при 2.98 гвей) — mute: пол бережёт от нуля",
      ex.volley_tier_gate_short("0xA002", int(0.0065 * ETH), 1_000_000, ESC_FEE) is True)

# 3. Admission эскалационного окна: сигнал — СЫРОЕ кольцо, не портфель слотов (линза (1))
import time as _t  # noqa: E402
_sv_ring = dict(ex._TRANSIT_TIPS)
ex._TRANSIT_TIPS.clear()
_nw = _t.time()
# НЕГАТИВНЫЙ КОНТРОЛЬ КЛАССА ДЕФЕКТА: эскалация 4.67 мгвей ЕСТЬ в кольце, а в слотах
# портфеля её НЕТ (между 1.775 и 12.76) — admission обязан видеть её ИЗ СЫРЬЯ
ex._TRANSIT_TIPS["0xagg"] = [(_nw - 5.0, 4_669_231)]
ex.transit_tip_portfolio = lambda now=None: (1_000_000, 1_775_000)   # слоты слепы
check("эскалация в КОЛЬЦЕ (между слотами!): B отсечена admission'ом",
      ex.volley_tier_b_esc_skip("0xB000") is True and ex._TIER_STATS["b_esc_skips"] == 1)
check("та же эскалация: A-нога выпускается", ex.volley_tier_b_esc_skip("0xA000") is False)
ex._TRANSIT_TIPS["0xagg"] = [(_nw - 300.0, 4_669_231)]               # протухшая запись
check("ПРОТУХШАЯ эскалация (300с > свежести 120с): B выпускается",
      ex.volley_tier_b_esc_skip("0xB000") is False)
ex._TRANSIT_TIPS["0xagg"] = [(_nw - 5.0, 1_775_000)]                 # свежий штиль
check("спокойное кольцо: B выпускается (fail-open в сторону плотности)",
      ex.volley_tier_b_esc_skip("0xB000") is False)
ex._TRANSIT_TIPS.clear()
check("пустое кольцо (сигнала нет): B выпускается", ex.volley_tier_b_esc_skip("0xB000") is False)
ex._TRANSIT_TIPS.update(_sv_ring)
ex.transit_tip_portfolio = _sv_ttp

# 4. НЕГАТИВНЫЙ КОНТРОЛЬ: спокойный класс — молчат ноль в ОБОИХ тирах
_before = dict(ex._TIER_STATS)
calm_mutes = sum(1 for a in A_LEGS + B_LEGS
                 if ex.volley_tier_gate_short(a, int(0.006 * ETH) if a in ex._TIER_A_SET
                                              else int(0.006 * ETH), 1_000_000, CALM_FEE))
check("спокойный класс: mute = 0 из 220 в обоих тирах", calm_mutes == 0, calm_mutes)

# 5. a_fired: огонь A считается, B — нет
ex.volley_tier_note_fire("0xA001")
ex.volley_tier_note_fire("0xB001")
check("a_fired считает только A-ноги", ex._TIER_STATS["a_fired"] == 1, ex._TIER_STATS)

# 6. Однотирный откат бит-в-бит: та же нога, тот же вход -> прежний вердикт формулы
_reset(a_legs=0)
old_ok = ex.volley_tier_gate_short("0xA000", int(0.006 * ETH), 1_000_000, CALM_FEE)
old_short = ex.volley_tier_gate_short("0xA000", int(0.004 * ETH), 1_000_000, P90_FEE)
check("откат: спокойный класс проходит, p90-эскалация глушит (прежняя семантика)",
      old_ok is False and old_short is True, (old_ok, old_short))

# ---- Гарды дрейфа боевых потребителей ----
_E = open("chain/execute.py").read()
_S = open("chain/spec_fire_live.py").read()
check("ОБА пол-гейта ходят через volley_tier_gate_short (пресигн + volley_probe)",
      _E.count("volley_tier_gate_short(addr, bal[0]") == 2)
check("admission эскалации стоит в ОБОИХ send-путях",
      _E.count("volley_tier_b_esc_skip(addr)") >= 2)
check("тир A = первые N ног keyfile (детерминированно)",
      "accs[:SPEC_FIRE_TIER_A_LEGS]" in _E)
check("B не переодевается в эскалационный tip (кап в presign_ladder)",
      "min(tip_wei, SPEC_FIRE_TIER_ESC_WEI)" in _E)
check("строка окна несёт tiers[a_mute/b_mute/a_fired/b_esc_skips]",
      "tiers[%s]" in _S and "b_esc_skips=%d" in _S)
check("откат одним env: дефолт 40, 0 = наследие",
      'os.environ.get("SPEC_FIRE_TIER_A_LEGS", "40")' in _E)

ex.SPEC_FIRE_TIER_A_LEGS = _sv_legs
ex._TIER_A_SET.clear()
ex._TIER_A_SET.update(_sv_set)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: два бакета — A держит эскалацию, B держит плотность, немота "
                     "перестала быть поголовной"))
sys.exit(1 if FAILS else 0)
