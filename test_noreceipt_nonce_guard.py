"""«Нет квитанции» != «нога сломана»: сверка nonce с цепью перед сбросом склада (#102).

ИНЦИДЕНТ 10.08, стоивший $600-900. Сборщик квитанций захлебнулся под плотным огнём и выдал
33 ложных NO-RECEIPT подряд. Каждый из них сбрасывал nonce и лестницу ноги, поэтому весь флот
ушёл в пересборку (110 ног, 28-32с), и окно молчания пришлось РОВНО на транзит: в блоках
49805091..49805095 наших tx НОЛЬ, а приз $10 390 ушёл конкуренту.

При этом транзакции САДИЛИСЬ. Замер по дельте nonce в блоках момента отправки
(49805060..49805075): с главного кошелька село 20 tx, с одной ноги 9. То есть сборщик не знал,
а код принял незнание за факт поломки — тот же класс, что «мёртвый сторож» и «хэш != эффект».

Цена ошибки несимметрична: сброс стоит 40 пред-подписанных проб и полминуты немоты ВСЕГО
флота, а лишняя сверка — один запрос nonce. Поэтому спрашиваем цепь ПЕРЕД сбросом.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                                   # noqa: E402
import chain.spec_fire_live as sfl                           # noqa: E402

ADDR = "0x" + "ab" * 20


class FakeRpc:
    def __init__(self, chain_nonce=None, boom=False):
        self.chain_nonce, self.boom = chain_nonce, boom

    def _web3(self):
        outer = self

        class _Eth:
            def get_transaction_count(self, a, blk=None):
                if outer.boom:
                    raise RuntimeError("узел не ответил")
                return outer.chain_nonce

        return type("W3", (), {"eth": _Eth()})()


def mk_sf(rpc):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._logs = []
    sf._log = sf._logs.append
    sf.stats = {}
    sf.rpc = rpc
    return sf


import threading                                          # noqa: E402

# volley_invalidate_nonce без замка ноги молча выходит — в бою замки заводятся при загрузке
# флота, в тесте создаём явно, иначе п.5 проверял бы не то.
ex._VOLLEY_LOCKS.setdefault(ADDR, threading.Lock())

_saved = dict(ex._VOLLEY_NONCE)
try:
    # --- 1. ГЛАВНОЕ: nonce на цепи ДОГНАЛ локальный -> склад СОХРАНЯЕМ ---------------------
    ex._VOLLEY_NONCE[ADDR] = 147
    assert ex.volley_leg_landed(FakeRpc(chain_nonce=147), ADDR) is True
    assert ex.volley_leg_landed(FakeRpc(chain_nonce=150), ADDR) is True, "обогнал — тем более село"
    print("OK 1/4: nonce на цепи догнал локальный -> tx села, сбрасывать нечего")

    # --- 2. nonce ОТСТАЁТ -> дыра реальна, сброс оправдан --------------------------------
    assert ex.volley_leg_landed(FakeRpc(chain_nonce=140), ADDR) is False, \
        "отставание nonce = настоящая дыра, склад обязан сброситься"

    # --- 3. НЕЗНАНИЕ закрывает гард: чтение упало / нет локального nonce -> сброс ---------
    assert ex.volley_leg_landed(FakeRpc(boom=True), ADDR) is False, \
        "ошибка чтения обязана вести к сбросу (лишняя пересборка дешевле дыры nonce)"
    ex._VOLLEY_NONCE.pop(ADDR, None)
    assert ex.volley_leg_landed(FakeRpc(chain_nonce=999), ADDR) is False, \
        "без локального nonce сверять не с чем — гард закрыт"
    print("OK 2/4: отставание и незнание -> сброс (несимметрия цены соблюдена)")

    # --- 4. СКВОЗНОЙ путь: _engine_timeout щадит живую ногу и считает это ----------------
    ex._VOLLEY_NONCE[ADDR] = 147
    sf = mk_sf(FakeRpc(chain_nonce=147))
    sf._engine_timeout((ADDR, None), "0xdead")
    assert sf.stats.get("noreceipt_spared") == 1, "живая нога не пощажена"
    assert not sf.stats.get("noreceipt_dropped"), "живую ногу всё равно сбросили"
    assert any("СОХРАНЁН" in m for m in sf._logs), "спасение склада молчит в журнале"
    assert ex._VOLLEY_NONCE.get(ADDR) == 147, "nonce сброшен у живой ноги"

    # --- 5. КОНТРОЛЬ: мёртвая нога ПО-ПРЕЖНЕМУ сбрасывается ------------------------------
    # Без этого п.4 неотличим от «мы просто выключили защиту от дыры nonce».
    ex._VOLLEY_NONCE[ADDR] = 147
    sf2 = mk_sf(FakeRpc(chain_nonce=140))
    sf2._engine_timeout((ADDR, None), "0xdead")
    assert sf2.stats.get("noreceipt_dropped") == 1, "нога с дырой не сброшена"
    assert not sf2.stats.get("noreceipt_spared")
    assert ex._VOLLEY_NONCE.get(ADDR) is None, "nonce отстающей ноги обязан пере-читаться"
    print("OK 3/4: сквозной путь — живая нога сохранена, отстающая сброшена")

    # --- 6. СТРУКТУРНО: сверка стоит ПЕРЕД сбросом, а не после ---------------------------
    # ЯКОРЬ ПЕРЕЕХАЛ (L1 р-8): денежная часть таймаута вынесена в `_engine_timeout_verdict`,
    # а `_engine_timeout` стал тонкой обёрткой «деньги в try, бронь #93 в finally». Берём ОБА
    # тела разом, иначе проверка молча смотрела бы в обёртку и не видела ни одного из двух
    # имён — то есть засчитывала бы порядок, которого не проверяла.
    src = pathlib.Path("chain/spec_fire_live.py").read_text()
    i = src.index("def _engine_timeout(")
    j = src.index("def _engine_timeout_verdict(")
    blk = src[i:src.index("\n    def ", j + 10)]
    assert blk.index("volley_leg_landed(") < blk.index("volley_invalidate_nonce("), \
        "сверка nonce стоит ПОСЛЕ сброса — то есть бесполезна"
    # L1 р-8: УЧЁТ НЕ СТОИТ ПЕРЕД ДЕНЬГАМИ. Вердикт победы обязан быть вызван РАНЬШЕ, чем
    # снятие брони #93: обратный порядок и был дефектом (бросок в учёте уносил wins, disarm,
    # закрытие окна и защиту ноги, а коллектор глотал его голым except).
    wrap = src[i:j]
    wrap = wrap[wrap.index('"""', wrap.index('"""') + 3):]      # докстринг не код: он тоже
    #                                                             называет обе строки, и по
    #                                                             нему «порядок» читался бы
    #                                                             из прозы, а не из вызовов
    assert "finally:" in wrap, "бронь #93 обязана сниматься в finally — иначе не на всех выходах"
    assert wrap.index("self._engine_timeout_verdict(") < wrap.index("finally:") \
        < wrap.index("volley_gas_settle("), \
        "снятие брони #93 стоит ПЕРЕД денежным вердиктом — учёт снова впереди денег"
    print("OK 4/4: сверка структурно предшествует сбросу; вердикт — перед снятием брони")
finally:
    ex._VOLLEY_NONCE.clear()
    ex._VOLLEY_NONCE.update(_saved)

print("=== #102 ЗАКРЫТ ===")
