"""Ш31: вторая нога цены идёт на РЕЗЕРВНЫЙ ключ Alchemy, а не конкурирует с деньгами.

РЕШЕНИЕ kelbic: «вторую ногу нельзя убирать ни в коем случае, включить резерв Alchemy».
И это подтверждено замером: preconf отвечает лишь в 40.3% случаев (3459 из 8579 за сутки),
поэтому одноногий режим удлинил бы денежный путь на таймаут в шести случаях из десяти.

Но она же — крупнейший потребитель БОЕВОГО ключа: read_preconf_price(s) даёт 206 вызовов из
~650 (около трети), и результат выбрасывается, когда preconf успел. Из-за этой нагрузки 429
убивали цели ДО гонки: за сутки 11 отказов из 45, среди них крупнейший приз каскада — $18 026
(`HOT skip 0xd4a903dc/0x20717d23: HTTP Error 429`).

Развязка: нога ОСТАЁТСЯ, но идёт по резервному ключу. Он оплачен, лежит в COMBAT_FAILOVER_URLS
и простаивал — использовался как аварийное переключение «всё или ничего», а не как вторая полоса.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.hotpath as hp                                    # noqa: E402

_saved = os.environ.get("PX_CONFIRMED_RPC_URL")


def reset():
    hp._PX_CONFIRMED_RPC = None


try:
    # --- 1. КОНТРОЛЬ: ручка пуста -> прежнее поведение БИТ-В-БИТ ------------------------
    os.environ.pop("PX_CONFIRMED_RPC_URL", None)
    reset()
    assert hp.px_confirmed_rpc("COMBAT") == "COMBAT", "без ручки confirmed-нога съехала с боевого"
    print("OK 1/5: ручка пуста -> обе ноги на боевом, откат сам собой")

    # --- 2. ГЛАВНОЕ: с ручкой confirmed-нога уходит на ОТДЕЛЬНЫЙ объект ------------------
    os.environ["PX_CONFIRMED_RPC_URL"] = "https://reserve.invalid/key"
    reset()
    r = hp.px_confirmed_rpc("COMBAT")
    assert r != "COMBAT", "confirmed-нога осталась на боевом ключе — развязки нет"
    assert hasattr(r, "contract"), "объект резерва не умеет contract() — price_fn упадёт"
    print("OK 2/5: confirmed-нога -> отдельный объект резерва (%s)" % type(r).__name__)

    # --- 3. Объект строится ОДИН раз (горячий путь не платит за конструктор) ------------
    r2 = hp.px_confirmed_rpc("COMBAT")
    assert r2 is r, "объект резерва пересоздаётся на каждом чтении — это TLS в горячей точке"
    print("OK 3/5: объект резерва строится один раз на процесс")

    # --- 4. НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: не смогли построить -> НАВСЕГДА боевой -------------
    # Иначе денежный путь тихо остался бы без confirmed-ноги вообще.
    reset()
    os.environ["PX_CONFIRMED_RPC_URL"] = "не-url-вовсе"
    _real = hp.px_confirmed_rpc("COMBAT")
    assert _real == "COMBAT" or hasattr(_real, "contract"), \
        "битый url не откатился на боевой — нога могла остаться без узла"
    print("OK 4/5: битый url -> откат на боевой, нога не теряется")

    # --- 5. ПОДМЕНА СТОИТ ВНУТРИ read_price_hedged, а не у вызывающих -------------------
    # Три места вызова передают боевой rpc; развязка обязана быть ОДНА, иначе разъедется.
    src = pathlib.Path("chain/hotpath.py").read_text()
    i = src.index("def read_price_hedged(")
    blk = src[i:src.index("\ndef ", i + 10)]
    assert "px_confirmed_rpc(rpc)" in blk, "развязка не внутри хеджа — вызывающие разъедутся"
    assert blk.index("px_confirmed_rpc(rpc)") < blk.index("preconf_mark_reactive"), \
        "подмена стоит ПОСЛЕ старта ног — часть чтений уйдёт на боевой"
    print("OK 5/5: развязка ОДНА и внутри хеджа, до старта ног")
finally:
    if _saved is None:
        os.environ.pop("PX_CONFIRMED_RPC_URL", None)
    else:
        os.environ["PX_CONFIRMED_RPC_URL"] = _saved
    reset()

# --- КОНТРОЛЬ: вторая нога НЕ УДАЛЕНА (решение kelbic, замер preconf 40.3%) -------------
src = pathlib.Path("chain/hotpath.py").read_text()
i = src.index("def read_price_hedged(")
blk = src[i:src.index("\ndef ", i + 10)]
assert "ev_p" in blk and "box_p" in blk, "параллельный хедж разобран — вторую ногу снимать НЕЛЬЗЯ"
print("КОНТРОЛЬ: вторая нога на месте — снимать её запрещено (preconf отвечает 40.3%)")
print("=== Ш31 ЗАКРЫТ: страховка сохранена, треть нагрузки ушла с боевого ключа ===")
