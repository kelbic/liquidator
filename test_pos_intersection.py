"""П.1/П.2 пакета kelbic 21.08: пересечение полоса∩класс ПО ХЭШАМ + метрика delta-по-match.

Урок 50262420 (ретро с цепи): полоса=9 и tip-match=42 в одной строке принадлежали РАЗНЫМ tx —
пересечение 0, вердикт «формула собрана» отозван. Два агрегатных счётчика НЕ доказывают
пересечение; писать вердикт имеет право только таблица по хэшам.

Фикстура ниже СПИСАНА С ЦЕПИ (блок 50262420, независимый ретро-проход), не с продукта:
транзит idx=127 tip=1775000; в полосе 9 наших НЕ на классе; 42 на классе с delta +72..+267.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


# ---- Стенд chain_index.our_position_table на фикстуре 50262420 ----
import chain.chain_index as ci  # noqa: E402

idx = ci.ChainIndex.__new__(ci.ChainIndex)
T_IDX, T_TIP = 127, 1775000
ours = {}
for k, i in enumerate(range(128, 137)):          # 9 в полосе, НЕ на классе (слоты 1.0/1.1)
    ours["0xband%02d" % k] = {"index": i, "tip": 1_000_000 if k % 2 else 1_100_000}
for k, i in enumerate(list(range(199, 213)) + list(range(261, 289))):   # 42 на классе, позади
    ours["0xmatch%02d" % k] = {"index": i, "tip": T_TIP}
_facts = {"ours": ours}
idx.block_facts = lambda bn: _facts if bn == 50262420 else None
idx.transit_report = lambda bn, agg=None: ("transit", {"index": T_IDX, "tip": T_TIP})

t = idx.our_position_table(50262420)
check("таблица построена", t is not None)
rows, (t_i, t_t) = t
check("транзит в кортеже", (t_i, t_t) == (T_IDX, T_TIP))
check("строк = всем нашим tx", len(rows) == 9 + 42, len(rows))
inter = [r for r in rows if 1 <= r[2] <= 20 and r[4]]
check("УРОК 50262420: пересечение = 0 при полосе 9 и match 42", len(inter) == 0, len(inter))
band = [r for r in rows if 1 <= r[2] <= 20]
match = [r for r in rows if r[4]]
check("полоса 9 (по индексу)", len(band) == 9, len(band))
check("match 42 (по tip)", len(match) == 42, len(match))
mm = sorted(r[2] for r in match)
check("метрика п.2: медиана delta по match ~+140 (baseline)", 100 < mm[len(mm)//2] < 220,
      mm[len(mm)//2])

# ---- Пересечение НЕ пустое, когда тот же tx в полосе И на классе ----
ours["0xwinner"] = {"index": T_IDX + 3, "tip": T_TIP}
rows2, _ = idx.our_position_table(50262420)
inter2 = [r for r in rows2 if 1 <= r[2] <= 20 and r[4]]
check("tx в полосе И на классе даёт пересечение=1 (и только он)",
      len(inter2) == 1 and inter2[0][0] == "0xwinner", inter2)
del ours["0xwinner"]

# ---- Отказы формы -> None ----
check("нет блока -> None", idx.our_position_table(1) is None)
idx.transit_report = lambda bn, agg=None: ("no_transit", None)
check("нет транзита -> None", idx.our_position_table(50262420) is None)

# ---- Тень-слот А1в3: ЗАКРЫТ 22.08 (ОПРОВЕРГНУТО 0/9) — по умолчанию строки НЕТ ----
import os as _os
idx.transit_report = lambda bn, agg=None: ("transit", {"index": T_IDX, "tip": T_TIP})
import chain.spec_fire_live as _sfl_mod
_o = _sfl_mod.SpecFireLive.__new__(_sfl_mod.SpecFireLive)
_o.stats = {}
_logs = []
_o._log = lambda m: _logs.append(m)
idx._rpc = lambda m, p: {"reward": [["0x%x" % 1_800_000]]}   # p50(B-1)=1.8 мгвей
_os.environ.pop("SHADOW_SLOT_A1V3", None)
_o._pos_table(idx, 50262420, None)
check("гейт 22.08: по умолчанию ТЕНЬ-СЛОТ не пишется (CU), вердикт жив",
      any("ПЕРЕСЕЧЕНИЕ" in l for l in _logs) and not any("ТЕНЬ-СЛОТ" in l for l in _logs),
      _logs[:2])
_logs.clear()
_os.environ["SHADOW_SLOT_A1V3"] = "1"
_o._pos_table(idx, 50262420, None)
_shadow = [l for l in _logs if "ТЕНЬ-СЛОТ" in l]
check("SHADOW_SLOT_A1V3=1 возвращает строку с ratio и hit-флагами",
      len(_shadow) == 1 and "ratio=1.01" in _shadow[0] and "hit10=True" in _shadow[0], _shadow)
_logs.clear()
idx._rpc = lambda m, p: (_ for _ in ()).throw(RuntimeError("узел упал"))
_o._pos_table(idx, 50262420, None)
check("отказ feeHistory глотается (вердикт и пересечение живут)",
      any("ПЕРЕСЕЧЕНИЕ" in l for l in _logs) and not any("ТЕНЬ" in l for l in _logs), _logs[:1])
_os.environ.pop("SHADOW_SLOT_A1V3", None)
idx._rpc = None

# ---- П.2б/в (26.08): ДВОЙНАЯ метрика + плотность полосы ----
_logs.clear()
idx._rpc = None
_o.stats = {}
_o._pos_table(idx, 50262420, None)
_near = [l for l in _logs if "БЛИЖАЙШАЯ" in l]
check("новая метрика: строка БЛИЖАЙШАЯ с delta/классом/чужими_между",
      len(_near) == 1 and "delta=+" in _near[0] and "чужих_между=" in _near[0], _near)
check("СТАРАЯ метрика не снята — ПЕРЕСЕЧЕНИЕ по-прежнему пишется",
      any("ПЕРЕСЕЧЕНИЕ" in l for l in _logs))
check("кольцо ближайших дельт копится отдельно от match-кольца",
      _o.stats.get("nearest_delta_ring") and _o.stats.get("nearest_n") == 1, _o.stats)
# фикстура: наша tx на transit+1 при нуле чужих между
ours["0xright_behind"] = {"index": T_IDX + 1, "tip": 1_000_000}
_logs.clear()
_o.stats = {}
_o._pos_table(idx, 50262420, None)
_n2 = [l for l in _logs if "БЛИЖАЙШАЯ" in l]
check("tx на transit+1: чужих_между=0 (мы фактически сразу за транзитом)",
      "delta=+1" in _n2[0] and "чужих_между=0" in _n2[0], _n2)
check("класс при этом «нет» — новая метрика НЕ требует равенства tip",
      "класс=нет" in _n2[0], _n2)
del ours["0xright_behind"]

# ---- Гарды дрейфа боевого потребителя (spec_fire_live) ----
import chain.spec_fire_live as sfl  # noqa: E402
_SRC = open(sfl.__file__.replace(".pyc", ".py")).read()
check("вердикт формулы пишет ТОЛЬКО пересечение (строка ПЕРЕСЕЧЕНИЕ)",
      "полоса∩класс=%d" in _SRC and "пишет ТОЛЬКО это число" in _SRC)
check("лог капирован (только delta -3..25 или match, максимум 60)",
      "[:60]" in _SRC and "(-3 <= r[2] <= 25) or r[4]" in _SRC)
check("кольцо delta-по-match капировано (~400)", "del ring[:-400]" in _SRC)
check("p50/p90 по match в закрытии окна", "match_delta=%s" in _SRC and "match_delta_ring" in _SRC)
check("прибор в треде индекса, не в recv-петле (внутри ветки rep)",
      "self._pos_table(ci, bn, agg)" in _SRC)
check("слепой суд таблицу не кормит", "if not blind:" in _SRC)

# ---- п.2/п.3 (26.08): метрика позиции в дайджест окна ----
_S3 = open("chain/spec_fire_live.py").read()
check("строка окна несёт позиция[первый_delta p50/p90 + чужих_между_ср + в_цели]",
      "позиция[%s]" in _S3 and "первый_delta p50=" in _S3 and "чужих_между_ср=" in _S3
      and "в_цели(+1..+5)=" in _S3)
check("метрика считается из кольца nearest_delta_ring (а не из воздуха)",
      'self.stats.get("nearest_delta_ring")' in _S3 and 'self.stats.get("nearest_n", 0)' in _S3)
check("анкер 50471608 назван в коде (наши +12..+20 против чужих +1..+11)",
      "50471608" in _S3 and "+12..+20" in _S3)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: вердикт формулы — только по хэшам; метрика delta-по-match в бою"))
sys.exit(1 if FAILS else 0)
