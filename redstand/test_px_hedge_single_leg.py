"""Вторая нога хеджа цены — только в деградацию (#101).

ЗАМЕР, ради которого правка. За 6 часов боевого журнала: `px_pre=1/1`, `2/2`, `3/3` — preconf
отвечает в 100% случаев, промахов НОЛЬ. Значит confirmed-нога, летевшая ПАРАЛЛЕЛЬНО в боевой
Alchemy, не пригодилась ни разу, а её результат выбрасывался. Это ровно половина `eth_call`
горячего пути — при том что `eth_call` даёт 74% суточного расхода (672.2K из 909.1K), скользящая
проекция 61-64M/мес против лимита 30M, и именно он топил боевой ключ: COMBAT-FAILOVER флапал
раз в 6 минут (24 за сутки, 10 за час), и число отказов шло ровно за нашим огнём
(13:00 — 9 отказов при 1862 пробах, 15:00 — 10 при 3958).

Теперь штатно идёт ОДНА нога с тем же таймаутом; промах включает деградацию на
PX_HEDGE_DEGRADE_SEC, и в ней возвращается прежний параллельный хедж бит-в-бит.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.hotpath as hp                                   # noqa: E402

ORACLE = "0x" + "aa" * 20
PRE, CONF = "preconf", "confirmed"
calls = []


def mk(pre_value, conf_value=777, pre_delay=0.0):
    """price_fn, считающий обращения по КАЖДОМУ узлу."""
    def _fn(r, oracle):
        calls.append(r)
        if r == PRE:
            if pre_delay:
                time.sleep(pre_delay)
            if isinstance(pre_value, Exception):
                raise pre_value
            return pre_value
        return conf_value
    return _fn


def reset():
    calls.clear()
    hp._PX_HEDGE_DEGRADED_UNTIL = 0.0
    hp.px_preconf_take()                    # обнулить счётчики окна


# --- 0. ГАРД: одноногая ветка ОБЯЗАНА уступать канал preconf (дефект, пойманный боем) --------
# Без preconf_mark_reactive() фоновый полл конкурирует с горячим чтением, preconf не
# укладывается в таймаут, и промахи выросли с НУЛЯ до 45% (px_pre 7/13, 10/18, 11/20).
# В одноногом режиме промах дороже: confirmed стартует ПОСЛЕ таймаута, а не параллельно.
_src = pathlib.Path("chain/hotpath.py").read_text()
_i = _src.index("if not px_hedge_degraded():")
_blk = _src[_i:_src.index("ev_p, box_p", _i)]
assert "preconf_mark_reactive()" in _blk, \
    "одноногая ветка не уступает канал preconf — вернутся 45% промахов"
print("OK 0/5: одноногая ветка уступает канал preconf (preconf_mark_reactive на месте)")

# --- 1. ГЛАВНОЕ: preconf здоров -> боевой узел НЕ трогаем вовсе ---------------------------
reset()
px = hp.read_price_hedged(PRE, CONF, ORACLE, price_fn=mk(123))
assert px == 123, px
assert calls == [PRE], "боевой узел дёрнут при живом preconf — половина eth_call снова впустую"
# px_preconf_take() отдаёт (ok, FAIL) — в журнал печатается ok/(ok+fail), отсюда «1/1».
ok, fail = hp.px_preconf_take()
assert (ok, fail) == (1, 0), (ok, fail)
print("OK 1/5: preconf ответил — обращений к боевому узлу НОЛЬ (было по одному на каждое чтение)")

# --- 2. Промах preconf -> добираем боевым И входим в деградацию ---------------------------
reset()
px = hp.read_price_hedged(PRE, CONF, ORACLE, price_fn=mk(None))
assert px == 777, "промах preconf, а цену с боевого не добрали: %s" % px
assert calls == [PRE, CONF], calls
assert hp.px_hedge_degraded() is True, "после промаха не включилась деградация"
print("OK 2/5: промах -> цена добрана боевым, включён режим обеих ног")

# --- 3. В ДЕГРАДАЦИИ работает прежний параллельный хедж (обе ноги) ------------------------
reset()
hp.px_hedge_note_miss()                     # мы в деградации
px = hp.read_price_hedged(PRE, CONF, ORACLE, price_fn=mk(555))
assert px == 555
assert sorted(calls) == [CONF, PRE], "в деградации обязаны идти ОБЕ ноги: %s" % calls
print("OK 3/5: в деградации — прежнее поведение бит-в-бит, обе ноги параллельно")

# --- 4. Деградация ИСТЕКАЕТ, и мы возвращаемся к одной ноге -------------------------------
reset()
hp.px_hedge_note_miss(now=time.monotonic() - hp._PX_HEDGE_DEGRADE_SEC - 1.0)
assert hp.px_hedge_degraded() is False, "режим деградации не истекает — экономия потеряна навсегда"
px = hp.read_price_hedged(PRE, CONF, ORACLE, price_fn=mk(321))
assert calls == [PRE], "после истечения деградации снова две ноги: %s" % calls
print("OK 4/5: деградация истекает через %.0fс — возвращаемся к одной ноге" % hp._PX_HEDGE_DEGRADE_SEC)

# --- 5. ОТКАТ одной строкой: PX_HEDGE_ALWAYS=1 -> прежнее поведение ----------------------
reset()
_saved = hp._PX_HEDGE_ALWAYS
try:
    hp._PX_HEDGE_ALWAYS = True
    assert hp.px_hedge_degraded() is True, "откат не возвращает обе ноги"
    hp.read_price_hedged(PRE, CONF, ORACLE, price_fn=mk(999))
    assert sorted(calls) == [CONF, PRE], calls
finally:
    hp._PX_HEDGE_ALWAYS = _saved
print("OK 5/5: PX_HEDGE_ALWAYS=1 возвращает обе ноги — откат без правки кода")

# --- КОНТРОЛЬ: латентная гарантия не изменилась ------------------------------------------
# Таймаут тот же _PX_HEDGE_SEC: медленный preconf не держит горячий путь дольше прежнего.
reset()
t0 = time.monotonic()
px = hp.read_price_hedged(PRE, CONF, ORACLE, price_fn=mk(42, pre_delay=hp._PX_HEDGE_SEC + 0.3))
dt = time.monotonic() - t0
assert dt < hp._PX_HEDGE_SEC + 0.35, "чтение заняло %.2fс — латентная гарантия нарушена" % dt
assert px == 777, "медленный preconf -> обязаны отдать боевую цену, отдали %s" % px
print("КОНТРОЛЬ: медленный preconf уложился в %.2fс (кап %.2f) и отдал цену с боевого" % (
    dt, hp._PX_HEDGE_SEC))
print("=== #101 ЗАКРЫТ ===")
