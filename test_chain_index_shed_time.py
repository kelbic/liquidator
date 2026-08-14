"""БЛОКЕР A (ревью р-6): диагноз «ЗАМОРОЖЕН» сравнивал ДВЕ РАЗНЫЕ ОСИ ВРЕМЕНИ.

ЧТО БЫЛО. Правка р-5 подавляла тревогу ДОЛЕЙ: `age <= shed_sec_win + STALE`. Но `age` — это
возраст последнего УСПЕШНОГО СКАНА (растёт БЕЗ ПОТОЛКА), а `shed_sec_win` — секунды уступки
ключа бою ЗА ОКНО отчёта (потолок = длина окна, 120с). При НЕПРЕРЫВНОЙ уступке возраст уходит
за 120с, остаток объявляется необъяснённым — и ERROR «ЗАМОРОЖЕН» печатается на ПРОЕКТНОЙ
НОРМЕ, ровно тот ложный диагноз, ради снятия которого закрывался блокер р-3 №5. Замер этой же
дрелью на коде р-5: уступка 240с → 1 ERROR, 600с → 4, 1200с → 9 (до правки р-5: 0/0/0).

ПОЧЕМУ ЭТОГО НЕ ЛОВИЛ СТЕНД. Позитивный контроль р-5 брал уступку 40-60с, то есть КОРОЧЕ окна
отчёта: режим отказа был недостижим ПО ПОСТРОЕНИЮ. Класс известен — «проверял в МОМЕНТЕ, а не
во ВРЕМЕНИ»; лечится он дрелью НЕПРЕРЫВНОГО состояния, которая здесь и стоит.

ЧТО ТЕПЕРЬ. Секунды уступки считаются С МОМЕНТА ПОСЛЕДНЕГО УСПЕШНОГО СКАНА (`_shed_ok_acc`
обнуляется рядом с `_last_ok` в `_ingest`) — на ТОЙ ЖЕ оси, что и `age`. Уступка, длящаяся всё
время протухания, объясняет его ЦЕЛИКОМ, сколько бы окон отчёта ни прошло; состояние «голова
растёт, скан уступает» названо СВОИМ именем и заморозкой не зовётся.

НАСТОЯЩИЕ здесь: `_loop`, `_catch_up`, `_confirmed_head`, `scan_block`, `_ingest`, `_health`,
`offer_block` в НАСТОЯЩЕМ треде. Подменены три шва: сокет, часы, шаг ожидания петли.
"""
import logging
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402

BN0 = 49890266
LOGGER = logging.getLogger("liquidator")


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errs, self.warns, self.infos = [], [], []

    def emit(self, rec):
        m = rec.getMessage()
        (self.errs if rec.levelno >= logging.ERROR
         else self.warns if rec.levelno >= logging.WARNING else self.infos).append(m)

    def all(self):
        return self.errs + self.warns + self.infos


def drill(seconds=600, shed_from=60.0, shed_to=None, freeze_after_sec=10 ** 6,
          feed_head=True, loud_sec=None):
    """`seconds` МОДЕЛЬНЫХ секунд жизни индекса.

    shed_from/shed_to — окно уступки ключа бою (shed_to=None => уступка НЕПРЕРЫВНА до конца).
    feed_head=True — recv-петля кладёт номер формируемого блока (offer_block), как в бою:
        именно по нему видно, что ЦЕПЬ ЖИВА, пока мы уступаем и ничего не читаем.
    freeze_after_sec — когда замирает ГОЛОВА УЗЛА (модель залипшего/мёртвого узла).
    """
    T = [1000.0]

    def _http(url, payload, timeout):
        m = payload["method"]
        elapsed = T[0] - 1000.0
        head = BN0 + int(min(elapsed, freeze_after_sec) // 2)     # блок Base = 2с
        if m == "eth_blockNumber":
            return {"result": hex(head)}
        if m == "eth_getLogs":
            return {"result": []}
        return {"result": None}

    idx = ci.ChainIndex("https://node.test/KEY", watch_to=["0x" + "ab" * 20], http=_http,
                        now=lambda: T[0], sleep=lambda s: None)
    idx._shed = lambda: (T[0] - 1000.0) >= shed_from and (
        shed_to is None or (T[0] - 1000.0) < shed_to)
    cap = Cap()
    cap.setLevel(logging.DEBUG)
    LOGGER.addHandler(cap)
    _sv_lvl = LOGGER.level
    LOGGER.setLevel(logging.INFO)
    _sv_poll = ci.CHAIN_INDEX_SELF_POLL_SEC
    _sv_loud = getattr(ci, "CHAIN_INDEX_SHED_LOUD_SEC", None)
    ci.CHAIN_INDEX_SELF_POLL_SEC = 1.0
    if loud_sec is not None and _sv_loud is not None:
        ci.CHAIN_INDEX_SHED_LOUD_SEC = float(loud_sec)

    class _Wake(object):
        """ЧАСЫ ПЕТЛИ ДВИГАЕТ САМ ТРЕД ИНДЕКСА (B4 р-7).

        Прежняя редакция шла из главного треда: `T += 1; _wake.set(); time.sleep(0.0015)` —
        то есть модельное время двигалось ПО РЕАЛЬНЫМ часам, в надежде, что настоящий тред
        успеет отработать оборот за 1.5мс. Под нагрузкой не успевал: 2 падения из 4 и ЛОЖНЫЙ
        «ЗАМОРОЖЕН» на проектной норме. Прибор, чьи показания зависят от загрузки машины,
        приёмкой быть не может. Теперь `_loop` крутится ЗДЕСЬ ЖЕ, а время идёт РОВНО ВНУТРИ
        его собственного ожидания: сколько бы ни длился реальный оборот, модель сдвинется на
        CHAIN_INDEX_SELF_POLL_SEC и ни на секунду больше. Настоящими остаются `_loop`,
        `_catch_up`, `scan_block`, `_ingest`, `_health`, `_selfcheck`, `offer_block`.
        """

        def wait(self, timeout=None):
            dt = float(timeout or 1.0)
            T[0] += dt
            if feed_head:
                # РЕЦВ-ПЕТЛЯ: подача ФОРМИРУЕМОГО блока. Сети здесь нет — O(1) присвоение, и
                # именно оно доказывает, что цепь жива, пока индекс уступает ключ бою.
                idx.offer_block(BN0 + int((T[0] - 1000.0) // 2) + 1)
            if (T[0] - 1000.0) >= seconds:
                idx._stop.set()
            return False

        def set(self):
            pass

        def clear(self):
            pass

        def is_set(self):
            return False

    idx._wake = _Wake()
    try:
        idx._stop.clear()
        idx._loop()
    finally:
        ci.CHAIN_INDEX_SELF_POLL_SEC = _sv_poll
        if _sv_loud is not None:
            ci.CHAIN_INDEX_SHED_LOUD_SEC = _sv_loud
        LOGGER.setLevel(_sv_lvl)
        LOGGER.removeHandler(cap)
    return idx, cap


def frozen_errs(cap):
    return [m for m in cap.errs if "ЗАМОРОЖЕН" in m]


if "--repro" in sys.argv:
    # РЕПРО-РЕЖИМ (числа для отчёта, без assert): сколько ложных ERROR даёт непрерывная уступка.
    for _sec in (240, 600, 1200):
        _i, _c = drill(seconds=_sec, shed_from=60.0)
        print("непрерывная уступка %4dс на ЖИВОЙ голове: ERROR ЗАМОРОЖЕН=%d, всего ERROR=%d, "
              "блоков=%d, возраст=%.0fс" % (_sec, len(frozen_errs(_c)), len(_c.errs),
                                            _i.stats["blocks"], _i.snapshot()["age"]))
    _i, _c = drill(seconds=1200, shed_from=10 ** 9, freeze_after_sec=60.0)
    print("ЗАМЕРШАЯ голова БЕЗ уступки 1200с: ERROR ЗАМОРОЖЕН=%d, stale_events=%d"
          % (len(frozen_errs(_c)), _i.stats["stale_events"]))
    sys.exit(0)


# ---------------------------------------------------------------- 1. ПРИЁМКА A (непрерывность)
# 1200 модельных секунд НЕПРЕРЫВНОЙ уступки при ЖИВОЙ растущей голове. Это проектная НОРМА:
# бой сидит на нашем ключе, читать мы не имеем права. Ни одного ERROR быть не должно.
idx_c, cap_c = drill(seconds=1200, shed_from=60.0)
_fr = frozen_errs(cap_c)
assert idx_c.stats["blocks"] > 0, "дрель бессмысленна, если индекс не читал вовсе: %s" % idx_c.stats
assert idx_c.snapshot()["fresh"] is False, "уступая, индекс обязан быть протухшим: %s" % idx_c.snapshot()
assert not _fr, ("НЕПРЕРЫВНАЯ уступка на ЖИВОЙ голове — проектная НОРМА, а не заморозка. "
                 "ERROR «ЗАМОРОЖЕН» за прогон: %d (%s)" % (len(_fr), _fr[:2]))
# D5B (вердикт р-7): ERROR под непрерывной уступкой РАЗРЕШЁН РОВНО ОДИН — эскалация по
# длительности. Уступка 100% дольше LOUD_SEC это состояние, в котором state()=unknown,
# corpses()=None, index_hits=0: приёмки ВСЕХ пакетов в нём неизмеримы, и это авария, а не
# норма. Блокер A р-6 (ложный «ЗАМОРОЖЕН» на проектной норме) проверяется строкой ВЫШЕ и
# остаётся закрытым: ни одного ERROR с этим диагнозом.
_not_esc = [m for m in cap_c.errs if "ЭСКАЛАЦИЯ" not in m]
assert not _not_esc, ("на проектной норме единственный законный ERROR — эскалация d5b: %s"
                      % _not_esc[:2])
assert cap_c.errs, ("уступка 1140с БЕЗ единого скана обязана быть ERROR (d5b): приёмки всех "
                    "пакетов в этом состоянии неизмерены, WARNING учит игнорировать строку")
# ...и это не молчание: состояние обязано быть НАЗВАНО своим именем в каждом окне.
# ВСЕ строки, а не только warns: после LOUD_SEC та же строка уходит уровнем ERROR (d5b).
_named = [m for m in cap_c.all() if "УСТУПКА КЛЮЧА БОЮ активна" in m]
assert len(_named) >= 8, ("уступка обязана называться в КАЖДОМ окне (1200с / 120с = 10), "
                          "иначе мы просто заменили ложную тревогу молчанием: %d (%s)"
                          % (len(_named), cap_c.warns[-1:]))
assert any("голова растёт" in m for m in _named), \
    ("у состояния «голова растёт, скан уступает» обязано быть СВОЁ имя — по нему читатель "
     "отличает уступку от заморозки: %s" % _named[-1])
print("UNIT_OK: непрерывная уступка 1200с на ЖИВОЙ голове — ложных «ЗАМОРОЖЕН» 0 (на коде "
      "р-5 было 9), "
      "состояние названо в %d окнах подряд" % len(_named))

# ---------------------------------------------------------------- 2. ПОЗИТИВНЫЙ КОНТРОЛЬ
# Та же дрель без уступки, но с ЗАМЕРШЕЙ головой узла: тревога ОБЯЗАНА звучать. Без этого
# пункта «ноль ERROR» выше не доказывает ничего — прибор мог просто онеметь.
idx_f, cap_f = drill(seconds=1200, shed_from=10 ** 9, freeze_after_sec=60.0)
_frf = frozen_errs(cap_f)
assert len(_frf) >= 1, ("ЗАМЕРШАЯ голова БЕЗ уступки обязана давать ERROR «ЗАМОРОЖЕН» — иначе "
                        "дрель ловит не то: %s" % cap_f.errs[:2])
assert idx_f.stats["stale_events"] >= 1, idx_f.stats
print("UNIT_OK: позитивный контроль — замершая голова БЕЗ уступки даёт %d ERROR «ЗАМОРОЖЕН» "
      "(stale_events=%d): гард умеет срабатывать" % (len(_frf), idx_f.stats["stale_events"]))

# ------------------------------------- 3. РЕГРЕСС: непрерывная уступка в 10 окон отчёта подряд
# Именно ПОВТОРНОСТЬ и ломала прежнюю редакцию: каждое следующее окно возраст рос, а объяснение
# упиралось в потолок окна. Считаем окна по строкам ЖИВОСТИ, а не по времени на глазок.
_win = [m for m in cap_c.all() if "CHAIN-INDEX ЖИВОСТЬ" in m]
assert len(_win) >= 9, "окон отчёта в прогоне 1200с обязано быть ~10: %d" % len(_win)
_ages = [float(x) for x in re.findall(r"возраст=([0-9.]+)с", " ".join(_win))]
assert _ages and max(_ages) > 4 * ci.CHAIN_INDEX_HEALTH_SEC, \
    ("возраст обязан УЙТИ далеко за длину окна — иначе режим отказа недостижим по построению "
     "(ровно этим и был слеп стенд р-5): max=%s" % (max(_ages) if _ages else None))
print("UNIT_OK: 10 окон подряд под непрерывной уступкой, возраст дошёл до %.0fс (> 4 окон), "
      "ложных «ЗАМОРОЖЕН» ноль — сравнение осей больше не даёт ложный диагноз" % max(_ages))

# ------------------- 4. УСТУПКА НЕ ЕСТЬ ВЕЧНЫЙ ОТКУП ЗА ПРОТУХАНИЕ, НАЧАВШЕЕСЯ ДО НЕЁ
# Голова узла замерла на 60-й секунде, уступка началась на 300-й и дальше НЕПРЕРЫВНА. 240
# секунд протухания уступкой НЕ объясняются ничем, и тревога обязана звучать — иначе мы
# закрыли блокер A ценой возврата майора A р-4 («касание боя подавляет ERROR вечно»).
idx_m, cap_m = drill(seconds=1200, shed_from=300.0, freeze_after_sec=60.0)
_frm = frozen_errs(cap_m)
assert len(_frm) >= 3, ("протухание, начавшееся ДО уступки, обязано звучать и ПОД уступкой: "
                        "ERROR=%d (%s)" % (len(_frm), cap_m.warns[-1:]))
assert any("объясняет лишь" in m for m in _frm), \
    ("тревога обязана НАЗЫВАТЬ долю: сколько секунд возраста уступка объясняет, а сколько нет: "
     "%s" % _frm[-1])
_un = re.search(r"объясняет лишь ([0-9.]+)с из ([0-9.]+)с", _frm[-1])
assert _un and (float(_un.group(2)) - float(_un.group(1))) >= 200.0, \
    ("необъяснённый остаток обязан быть ровно тем, что мы НЕ читали и НЕ уступали (~240с): %s"
     % _frm[-1])
print("UNIT_OK: протухание ДО уступки (240с) звучит ERROR-ом %d раз даже под НЕПРЕРЫВНОЙ "
      "уступкой — остаток назван числом (%s из %s)" % (len(_frm), _un.group(1), _un.group(2)))

# ---------------------------------------------------------------- 5. МАЙОР М1: ЭСКАЛАЦИЯ
# Прежняя эскалация стояла на ЧИСЛЕ ОКОН и была мертва дважды: `_shed_supp_streak` обнулялся
# ДО печати (в журнале всегда «0 окон подряд»), а порог N=3 недостижим ни в одном режиме
# (max streak=1). Теперь порог — ДЛИТЕЛЬНОСТЬ уступки БЕЗ СКАНА, и он проверяется мутацией.
_esc = [m for m in cap_c.all() if "ЭСКАЛАЦИЯ" in m]      # d5b: уровень ERROR, см. п.1
assert _esc, ("непрерывная уступка 1140с обязана эскалироваться (порог %.0fс): %s"
              % (ci.CHAIN_INDEX_SHED_LOUD_SEC, cap_c.warns[-1:]))
_num = re.search(r"ЭСКАЛАЦИЯ: уступка без единого скана уже (\d+)с", _esc[-1])
assert _num and int(_num.group(1)) >= int(ci.CHAIN_INDEX_SHED_LOUD_SEC), \
    ("эскалация обязана печатать НАСТОЯЩУЮ длительность, а не ноль (дефект М1: streak "
     "обнулялся ДО печати): %s" % _esc[-1])
idx_e, cap_e = drill(seconds=1200, shed_from=60.0, loud_sec=10 ** 9)     # МУТАЦИЯ ПОРОГА
assert not [m for m in cap_e.all() if "ЭСКАЛАЦИЯ" in m], \
    ("мутация порога (LOUD_SEC=1e9) ОБЯЗАНА снять эскалацию — иначе строка печатается всегда "
     "и порога не держит: %s" % [m for m in cap_e.warns if "ЭСКАЛАЦИЯ" in m][:1])
assert not cap_e.errs, "мутация порога не имеет права рождать ERROR: %s" % cap_e.errs[:2]
print("UNIT_OK: эскалация живёт на ДЛИТЕЛЬНОСТИ (%sс), печатает настоящие секунды, и мутация "
      "порога её снимает — значит порог держит" % _num.group(1))

# ---------------------------------------------------------------- 6. МАЙОР М3: ДЫРА ОТ УСТУПКИ
# Прыжок догона MAX_CATCHUP после уступки — это НАШ пропуск по проектной норме, а не дыра узла.
# Прежде он шёл в общий `gaps` и дисквалифицировал строку «ЧИТАЮ ЦЕПЬ» на боевом ритме #100
# (уступка чаще раза в 4 минуты => «ЧИТАЮ ЦЕПЬ» недостижимо в принципе).
idx_g, cap_g = drill(seconds=900, shed_from=120.0, shed_to=240.0)
assert idx_g.stats.get("gaps_shed", 0) >= 1, \
    ("пропуск догона ПОСЛЕ уступки обязан считаться отдельным именем: %s" % idx_g.stats)
assert idx_g.stats.get("gaps", 0) == 0, \
    ("узел в этой дрели исправен — дыр УЗЛА быть не должно: %s" % idx_g.stats)
_alive_after = [m for m in cap_g.infos if "ЧИТАЮ ЦЕПЬ" in m]
assert _alive_after, ("после возврата ключа индекс обязан вернуться к «ЧИТАЮ ЦЕПЬ»: уступочный "
                      "пропуск не имеет права дисквалифицировать строку живости: %s"
                      % cap_g.warns[-2:])
assert any("пропусков по уступке" in m for m in cap_g.all()), \
    "уступочный пропуск обязан быть НАЗВАН в строке, а не растворён: %s" % cap_g.warns[-1:]
print("UNIT_OK: пропуск догона от уступки учтён отдельно (gaps_shed=%d, gaps узла=%d) и не "
      "мешает «ЧИТАЮ ЦЕПЬ» (%d строк)" % (idx_g.stats.get("gaps_shed", 0),
                                          idx_g.stats.get("gaps", 0), len(_alive_after)))

print("ПРОШЛИ: уступка и заморозка считаются на ОДНОЙ оси времени; непрерывное состояние "
      "проверено дрелью, а не моментом")
