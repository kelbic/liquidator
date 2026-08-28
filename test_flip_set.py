"""27.08 НЕДОСТАЮЩИЙ КУСОК: доска заряжается ВСЕМИ, кого перекроет ожидаемый ход.

ЗАМЕР, из которого это выросло (каскад 26.08, блок 50483683):
  на нашей доске стоял ОДИН заёмщик HF 1.02319 = 2.27% от кромки (выбран как самый жирный);
  десять победителей стояли в пределах 0.51%, и это были ДЕСЯТЬ РАЗНЫХ жертв одного оператора;
  арм брал top-1 в статическом окне 3%, то есть ни близость, ни множественность не учитывались.
Собрано из двух готовых деталей: ret_fn (идущее движение оффчейн-цены, упреждение 70-160с по
замеру PREDICT) и hf_by (карта здоровья, свежесть 75с). Формула досягаемости — ТА ЖЕ, что уже
стоит в FIRE-гейте: |ret| + MARGIN_DEVS x dev. Новых данных и новой сети не требуется.

Стенд зовёт БОЕВОЙ _flip_set, а не его копию.
"""
import sys, threading, time, types
sys.path.insert(0, ".")
import chain.spec_fire_live as sfl
import chain.push_scale as ps

MID = "0xm"


def _floor(pct):
    """Задать НИЖНЮЮ границу рамки замером толчков: рамка = max(ход, замер), и без контроля
    над замером стенд проверял бы не то (первый прогон 27.08 упал именно на этом: семя BTC
    0.530% оказалось шире хода 0.40%)."""
    ps._RINGS.clear()
    now = time.time()
    for i in range(ps.MIN_N + 5):
        ps.note_push("BTC", pct, now=now - i)


def _sf(hf_by, ret, dev=0.001):
    m = types.SimpleNamespace(collateral_token="cbBTC", loan_token="USDC", bonus=0.05)
    sh = types.SimpleNamespace(
        lock=threading.Lock(), groups={MID: list(hf_by)},
        debt_by={(MID, b): 1_000_000.0 - i for i, b in enumerate(hf_by)},
        hf_by={(MID, b): v for b, v in hf_by.items()}, by_id={MID: m})
    sf = object.__new__(sfl.SpecFireLive)
    sf.shared = sh
    sf.stats = {}
    sf._unbuildable, sf._unbuildable_mid = {}, {}
    sf._now = lambda: time.time()
    sf._log = lambda *a, **k: None
    sf.ret_fn = (lambda f: ret) if ret is not None else (lambda f: None)
    sf.dev_by = {"BTC": dev}
    return sf


# цели: 0.05%, 0.10%, 0.30%, 0.90%, 2.20% от кромки (hf = 1/(1-d))
D = {"0xA": 0.0005, "0xB": 0.0010, "0xC": 0.0030, "0xD": 0.0090, "0xE": 0.0220}
HF = {b: 1.0 / (1.0 - d) for b, d in D.items()}

# --- 1. ПОЗИТИВНЫЙ КОНТРОЛЬ: механизм вообще отбирает, и отбирает МНОЖЕСТВО ---------------
# 28.08: семя push_scale стало НИЖНЕЙ границей (BTC 0.530%), поэтому «узкий замер» больше не
# сужает рамку ниже семени. Чтобы рамку по-прежнему задавал ХОД, ход обязан быть ШИРЕ семени:
# 0.80% + 2x0.10% = 1.00% > 0.530%. Порог целей переставлен: внутри 1.00% лежат A,B,C,D.
_floor(0.05)                                   # замер узкий -> ниже семени -> рамку задаёт ХОД (он шире)
sf = _sf(HF, ret=-0.0080, dev=0.001)          # ход 0.80% + 2x0.1% = досягаемость 1.00%
best, hold, edge, reach, src = sf._flip_set("BTC", "down", k=8)
got = {t[1] for t in best}
assert src.startswith("ход") and abs(reach - 0.0100) < 1e-9, (src, reach)
assert got == {"0xA", "0xB", "0xC", "0xD"}, ("внутри 1.00%% обязаны быть четыре цели: %s" % (got,))
assert len(best) == 4, "доска обязана заряжаться ВСЕМИ, а не одной"
print("UNIT_OK 1: ход 0.80%% -> досягаемость 1.00%% -> отобраны ВСЕ четыре цели внутри неё")

# --- 2. ГЛАВНОЕ: цель каскада (2.27%) НЕ попадает, а близкие попадают ---------------------
assert "0xE" not in got, "цель на 2.20%% при ходе 0.80%% в набор попадать не должна"
print("UNIT_OK 2: цель на 2.20%% отсеяна — ровно тот случай, что стоял на доске 26.08")

# --- 3. ХОД БОЛЬШЕ -> НАБОР ШИРЕ (механизм следит за движением, а не за константой) -------
_floor(0.05)
sf2 = _sf(HF, ret=-0.0210, dev=0.001)         # ход 2.1% + 0.2% = 2.30% -> захватывает и 0xE (2.20%, не на кромке)
best2, _, _, reach2, _ = sf2._flip_set("BTC", "down", k=8)
assert abs(reach2 - 0.0230) < 1e-9 and {t[1] for t in best2} == {"0xA", "0xB", "0xC", "0xD", "0xE"}, (reach2, best2)
print("UNIT_OK 3: ход 2.10%% -> досягаемость 2.30%% -> в наборе стало пять целей (ход шире -> набор шире)")

# --- 3б. ЗАМЕР ШИРЕ ХОДА -> рамку задаёт ЗАМЕР (иначе при затишье доска пуста и мы НЕ ГОТОВЫ
#         к моменту, когда ход вырастет: арм идёт по цепи и мгновенным не бывает)
_floor(0.95)                                   # типичный толчок 0.95% (не на самой границе цели 0.90%)
sfb = _sf(HF, ret=-0.0001, dev=0.00001)       # ход почти нулевой
bestb, _, _, reachb, srcb = sfb._flip_set("BTC", "down", k=8)
assert abs(reachb - 0.0095) < 1e-9, ("рамку обязан задать ЗАМЕР 0.95%%: %s" % (reachb,))
assert {t[1] for t in bestb} == {"0xA", "0xB", "0xC", "0xD"}, bestb
print("UNIT_OK 3б: ход ~0 -> рамку держит замеренный толчок 0.95%%, доска НЕ пустеет")

# --- 3в. ХОДА НЕТ, НО ЗАМЕР ЕСТЬ -> рамку задаёт замер (отдельная ветка кода от 3б!) -----
_floor(0.95)
sfc = _sf(HF, ret=None)                        # ret_fn молчит, но кольцо толчков живое
bestc, _, _, reachc, srcc = sfc._flip_set("BTC", "down", k=8)
assert abs(reachc - 0.0095) < 1e-9, ("без хода рамку обязан держать ЗАМЕР: %s" % (reachc,))
assert srcc == "незнание+замер", srcc
assert {t[1] for t in bestc} == {"0xA", "0xB", "0xC", "0xD"}, bestc
print("UNIT_OK 3в: хода нет, замер 0.95%% -> рамка 0.95%%, а не прежние 3%% (ветка незнания)")

# --- 4. НЕЗНАНИЕ НЕ СУЖАЕТ ОКНО (пустая доска = немой флот, это дороже) -------------------
ps._RINGS.clear(); ps.PUSH_SCALE_ON = False    # ни хода, ни замера -> прежний потолок
sf3 = _sf(HF, ret=None)
best3, _, _, reach3, src3 = sf3._flip_set("BTC", "down", k=8)
assert src3.startswith("незнание") and reach3 == sfl.SPEC_FIRE_ARM_MAX_DIST, (src3, reach3)
assert len(best3) == 5, "при незнании берём прежний статический потолок бит-в-бит"
print("UNIT_OK 4: ret_fn молчит и замера нет -> прежний потолок %.0f%%, все пять (бит-в-бит)" % (reach3 * 100))
ps.PUSH_SCALE_ON = True

# --- 5. ПОТОЛОК НЕ ПРЕВЫШАЕТСЯ: огромный ход не расширяет окно шире прежнего -------------
_floor(0.05)
sf4 = _sf(HF, ret=-0.50, dev=0.01)
_, _, _, reach4, _ = sf4._flip_set("BTC", "down", k=8)
assert reach4 == sfl.SPEC_FIRE_ARM_MAX_DIST, "шире прежнего потолка не берём никогда"
print("UNIT_OK 5: ход 50%% не расширяет окно шире прежних %.0f%%" % (reach4 * 100))

# --- 6. ОТКАТ ОДНОЙ РУЧКОЙ ---------------------------------------------------------------
_saved = sfl.SPEC_FIRE_FLIP_SET
sfl.SPEC_FIRE_FLIP_SET = False
_floor(0.05)
_, _, _, reach5, src5 = _sf(HF, ret=-0.0020)._flip_set("BTC", "down", k=8)
assert src5 == "off" and reach5 == sfl.SPEC_FIRE_ARM_MAX_DIST
sfl.SPEC_FIRE_FLIP_SET = _saved
print("UNIT_OK 6: SPEC_FIRE_FLIP_SET=0 возвращает прежнее поведение")

# --- 7. ПРОВОДКА: арм-путь зовёт ИМЕННО его и печатает порог, по которому судил -----------
src = open("chain/spec_fire_live.py").read()
assert "self._flip_set(" in src, "арм-путь не зовёт _flip_set"
assert "k=SPEC_FIRE_FLIP_SET_K" in src, \
    "арм-путь обязан брать ВЕСЬ набор (k=SPEC_FIRE_FLIP_SET_K), а не одну цель"
i = src.index("arm-skip feed=%s")
assert "досягаемость %.2f%%" in src[i:i + 400], "тревога обязана нести ПОРОГ, по которому судила"
assert "_top_fat(feed, direction, max_dist=SPEC_FIRE_ARM_MAX_DIST)" not in src, \
    "старый статический вызов остался в арм-пути"
print("UNIT_OK 7: проводка — арм зовёт _flip_set, arm-skip несёт свой порог")
print("=== ФЛИП-НАБОР: 7/7 ===")
