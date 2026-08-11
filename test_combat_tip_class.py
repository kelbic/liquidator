"""Ш28: боевой tip платит ИЗМЕРЕННЫЙ класс, а не 312 крат поля.

ЖИВАЯ ГОНКА 11.08 14:51, вскрытие по цепи:
  * победитель — блок 49835262, idx 309, tip 1 600 000 wei = РОВНО доминирующий класс блока
    (75.2% транзакций там же);
  * мы — блок 49835264 (на ДВА блока позже), idx 99, tip 500 000 000 wei = 0.5 gwei = 312x поля.
  * приз: три цели, крупнейшая $15 594 погашения при net >= $207; наш итог −$0.11 и −$0.47x2.

Переплата не покупает место: внутри суб-блока порядок по УБЫВАНИЮ tip, поэтому она
телепортирует в ГОЛОВУ — туда, где цена ещё старая и позиция здорова. Пробы в класс привели
(Ш24), боевой путь оставался на 0.05/0.5 gwei.
"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.hotpath as hp                                    # noqa: E402

cfg = types.SimpleNamespace(tip_gwei=0.05, tip_step_gwei=0.5, gas_limit_est=500_000,
                            eth_price_usd=1872.0)


def reset():
    hp._DOM_TIP_WEI = 0
    hp._DOM_TIP_TS = 0.0


# --- 1. ГЛАВНОЕ: измеренный класс вытесняет и базу, и каскадную ступень -------------------
reset()
hp.note_dominant_tip(1_600_000, now=100.0)
got = hp.dynamic_tip_gwei(cfg, 15594.0, now=100.0)
assert abs(got - 0.0016) < 1e-9, "боевой tip %.6f gwei вместо измеренного класса 0.0016" % got
print("OK 1/6: платим класс 0.0016 gwei вместо 0.05 (было 31x поля)")

# --- 2. В КАСКАДЕ тоже класс: победитель 14:51 стоял на классе ВО ВРЕМЯ каскада -----------
reset()
hp.note_dominant_tip(1_600_000, now=100.0)
_saved_casc = hp.cascade_active
try:
    hp.cascade_active = lambda now=None: True
    got = hp.dynamic_tip_gwei(cfg, 15594.0, now=100.0)
    assert abs(got - 0.0016) < 1e-9, \
        "в каскаде вернулась ступень 0.5 gwei (312x поля) — тот самый промах"
finally:
    hp.cascade_active = _saved_casc
print("OK 2/6: в каскаде тоже класс, ступень 0.5 gwei больше не вылезает")

# --- 3. НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: класса нет -> прежнее поведение бит-в-бит ----------------
reset()
assert hp.dominant_tip_wei(now=100.0) == 0
got = hp.dynamic_tip_gwei(cfg, 100.0, now=100.0)
assert abs(got - 0.05) < 1e-9, "без измерения tip обязан быть прежним, получено %.4f" % got
print("OK 3/6: класс не измерен -> прежние 0.05 gwei (откат сам собой)")

# --- 4. ПРОТУХАНИЕ: старое значение НЕ используется ---------------------------------------
reset()
hp.note_dominant_tip(1_600_000, now=100.0)
assert hp.dominant_tip_wei(now=100.0 + hp._DOM_TIP_TTL + 1.0) == 0, \
    "протухший класс возвращается — платили бы вчерашней ценой"
got = hp.dynamic_tip_gwei(cfg, 100.0, now=100.0 + hp._DOM_TIP_TTL + 1.0)
assert abs(got - 0.05) < 1e-9
print("OK 4/6: класс протухает через %.0fс и откатывает на прежнее" % hp._DOM_TIP_TTL)

# --- 5. АНОМАЛИИ ОТБРАСЫВАЮТСЯ, а не клэмпятся -------------------------------------------
# Клэмп превратил бы сбой узла в «правдоподобную» цену на ДЕНЕЖНОМ пути.
reset()
assert hp.note_dominant_tip(999) is False, "мусор ниже пола принят"
assert hp.note_dominant_tip(900_000_000) is False, "выброс 0.9 gwei принят"
assert hp.dominant_tip_wei(now=100.0) == 0, "аномалия просочилась в кэш"
assert hp.note_dominant_tip(1_775_000, now=100.0) is True, "здоровое значение отвергнуто"
print("OK 5/6: аномалии отброшены (не клэмп — на денежном пути это опаснее)")

# --- 6. ОТКАТ одной переменной ------------------------------------------------------------
reset()
hp.note_dominant_tip(1_600_000, now=100.0)
_s = hp._COMBAT_TIP_LEGACY
try:
    hp._COMBAT_TIP_LEGACY = True
    assert abs(hp.dynamic_tip_gwei(cfg, 100.0, now=100.0) - 0.05) < 1e-9, "откат не работает"
finally:
    hp._COMBAT_TIP_LEGACY = _s
print("OK 6/6: COMBAT_TIP_LEGACY=1 возвращает прежнее поведение")

# --- СТРУКТУРНО: семплер ЗАВЕДЁН, иначе правка мертва ------------------------------------
# Без вызова start_tip_sampler кэш всегда пуст, dominant_tip_wei() всегда 0, и весь пакет
# молча работает по-старому — ровно тот шов, под которым арм доски прожил мёртвым пять суток.
m = pathlib.Path("main.py").read_text()
assert "start_tip_sampler(" in m, "семплер НЕ заведён в main.py — правка мертва по построению"
i = m.index("start_tip_sampler(")
assert "BG_RPC_URL" in m[i:i + 400], "семплер ходит по БОЕВОМУ ключу — это диагностика, не деньги"
print("СТРУКТУРНО: семплер заведён в main.py и ходит по bg-стрипу")
print("=== Ш28 ЗАКРЫТ: денежный путь платит класс, а не 312 крат поля ===")
