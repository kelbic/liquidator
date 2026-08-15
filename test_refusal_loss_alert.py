"""Дрель #123: «отказ при чужой победе» обязан сообщать о себе.

Вопрос kelbic 14.08: «разве о проигранной гонке cbADA не должен был прийти отчёт в tg?»
Разбор: уведомитель Ш16 подключён к рентгену ОТПРАВЛЕННЫХ tx — отказы (sim-revert -> HOT skip)
невидимы ему по построению; 10 отказов за 7 дней прошли молча.

Канон «тесты не трогают боевые каналы»: алертер здесь ФЕЙКОВЫЙ (шов на транспорте), класс
Alerter не конструируется, сеть не трогается. Канон «шов не выше дефекта»: проверяем не только
чистые функции, но и что боевые точки (prepare_hot/prepare_fast/_ingest/main) реально проведены.
"""
import os
import sys
import time

os.environ.setdefault("REFUSAL_LOSS_ALERT", "1")

import chain.hotpath as hp                                    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    tag = "OK" if cond else "FAIL"
    print("%s %s%s" % (tag, name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class FakeAlerter:
    def __init__(self):
        self.sent = []

    def send_async(self, text, key="default"):
        self.sent.append((key, text))


class FakeCfg:
    liquidator_address = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
    wallet_address = "0xe2aca8e68ae3b7efcfce4fd82abdfc2a9d7908fe"


MID = "0xD7520AD1" + "ab" * 28                                # 0x + 64 hex, нарочно ВЕРХНИЙ регистр
BORROWER = "0x47E2aD2C70C3C75F6dc3F586996d4CF7F0C163fe"      # checksum-форма (Morpho API)
WINNER = "0x81c8654d000000000000000000000000000000aa"


def fresh():
    hp._REFUSAL_REG.clear()
    return FakeAlerter(), FakeCfg()


# --- 1. ПОЗИТИВ: отказ записан checksum-формами, событие пришло lower (форма цепи) -> алерт ---
al, cfg = fresh()
hp._note_refusal(MID, BORROWER, "panic-underflow", 142.0)
cb = hp.make_refusal_loss_watcher(al, cfg)
ev = {"mid": MID.lower(), "borrower": BORROWER.lower(), "caller": WINNER, "tx": "0xdeadbeef01"}
cb(49965023, [ev])
check("позитив: алерт ушёл", len(al.sent) == 1, "sent=%r" % al.sent)
if al.sent:
    k, t = al.sent[0]
    check("позитив: ключ refusal-loss", k == "refusal-loss", k)
    check("позитив: тело содержит рынок/бакет/победителя",
          MID.lower()[:10] in t and "panic-underflow" in t and WINNER[:12] in t, t)
check("позитив: запись выбыла (один алерт на отказ)", len(hp._REFUSAL_REG) == 0)

# --- 2. НЕГАТИВ: пустой регистр -> тишина ---
al, cfg = fresh()
hp.make_refusal_loss_watcher(al, cfg)(49965024, [ev])
check("негатив: без отказа тишина", len(al.sent) == 0)

# --- 3. НЕГАТИВ: TTL истёк -> тишина, запись вычищена ---
al, cfg = fresh()
hp._REFUSAL_REG[(MID.lower(), BORROWER.lower())] = (time.time() - hp.REFUSAL_TTL_SEC - 5,
                                                    "panic-underflow", 142.0)
hp.make_refusal_loss_watcher(al, cfg)(49965025, [ev])
check("негатив: TTL истёк — тишина", len(al.sent) == 0)
check("негатив: TTL истёк — запись вычищена", len(hp._REFUSAL_REG) == 0)

# --- 4. НЕГАТИВ: своя победа (наш контракт как caller) -> тишина ---
al, cfg = fresh()
hp._note_refusal(MID, BORROWER, "net-below-min", 500.0)
ev_self = dict(ev, caller=cfg.liquidator_address.lower())
hp.make_refusal_loss_watcher(al, cfg)(49965026, [ev_self])
check("негатив: своя победа — тишина", len(al.sent) == 0)

# --- 5. ОТКАТ: REFUSAL_LOSS_ALERT=0 глушит ЗАПИСЬ (регистр пуст -> каналу нечего сказать) ---
al, cfg = fresh()
_saved = hp.REFUSAL_LOSS_ALERT
hp.REFUSAL_LOSS_ALERT = False
try:
    hp._note_refusal(MID, BORROWER, "panic-underflow", 142.0)
    check("откат: регистр не пишется", len(hp._REFUSAL_REG) == 0)
finally:
    hp.REFUSAL_LOSS_ALERT = _saved

# --- 6. ПРИБОР НЕ РОНЯЕТ ВЫЗЫВАЮЩЕГО: кривое событие -> без исключения ---
al, cfg = fresh()
hp._note_refusal(MID, BORROWER, "panic-underflow", 142.0)
try:
    hp.make_refusal_loss_watcher(al, cfg)(0, [None, {}, {"mid": 5}])
    check("живучесть: мусорные события не роняют колбэк", True)
except Exception as e:
    check("живучесть: мусорные события не роняют колбэк", False, repr(e))

# --- 7. ПРОВОДКА (шов не выше дефекта): боевые точки реально ходят через прибор ---
src_hot = open(os.path.join(os.path.dirname(hp.__file__), "hotpath.py")).read()
i_revert = src_hot.find('bucket=%s err=%s | repaid_shares')            # DIAG sim-revert в prepare_hot
check("проводка: sim-revert пишет отказ",
      i_revert > 0 and "_note_refusal(market_id, borrower, _diag_bucket" in src_hot[i_revert - 800:i_revert])
check("проводка: net-below-min пишет отказ", '_note_refusal(market_id, borrower, "net-below-min"' in src_hot)
check("проводка: fast-net пишет отказ", '_note_refusal(market_id, borrower, "fast-net-below-min"' in src_hot)
src_idx = open(os.path.join(os.path.dirname(hp.__file__), "chain_index.py")).read()
check("проводка: индекс зовёт подписчика в _ingest", "_cb(bn, _liq)" in src_idx and "set_on_liquidate" in src_idx)
src_main = open(os.path.join(os.path.dirname(os.path.dirname(hp.__file__)), "main.py")).read()
check("проводка: арм ЯВНЫЙ в main.py", "set_on_liquidate" in src_main and "make_refusal_loss_watcher" in src_main)

# --- 8. Дефолт боевой: канал включён из коробки (иначе дыра Ш16 остаётся) ---
check("дефолт: REFUSAL_LOSS_ALERT включён", hp.REFUSAL_LOSS_ALERT is True)

# --- 9. ОДНОБЛОЧНАЯ ГОНКА (дыра каскада cbXRP 22:23): победа пришла ДО записи отказа ---
al, cfg = fresh()
hp._RECENT_LIQ.clear()
cb = hp.make_refusal_loss_watcher(al, cfg)              # армит _RL_SINK
cb(49978436, [ev])                                      # победа обработана ПЕРВОЙ (регистр пуст)
check("одноблочная: победа в кольце, алертов пока 0", len(al.sent) == 0 and len(hp._RECENT_LIQ) == 1)
hp._note_refusal(MID, BORROWER, "panic-underflow", 24789.0)   # отказ записан ПОСЛЕ
check("одноблочная: встречный матч дал алерт", len(al.sent) == 1, repr(al.sent))
if al.sent:
    check("одноблочная: тело содержит победителя и бакет",
          WINNER[:12] in al.sent[0][1] and "panic-underflow" in al.sent[0][1])
check("одноблочная: запись выбыла (один алерт)", len(hp._REFUSAL_REG) == 0)

# --- 10. Одноблочная: СВОЯ победа в кольце — тишина ---
al, cfg = fresh()
hp._RECENT_LIQ.clear()
cb = hp.make_refusal_loss_watcher(al, cfg)
cb(1, [dict(ev, caller=cfg.liquidator_address.lower())])
hp._note_refusal(MID, BORROWER, "net-below-min", 100.0)
check("одноблочная: своя победа — тишина", len(al.sent) == 0)

# --- 11. Одноблочная: победа старше окна REFUSAL_RECENT_WIN_SEC — тишина ---
al, cfg = fresh()
hp._RECENT_LIQ.clear()
hp.make_refusal_loss_watcher(al, cfg)
hp._RECENT_LIQ.append((time.time() - hp.REFUSAL_RECENT_WIN_SEC - 5,
                       MID.lower(), BORROWER.lower(), WINNER, "0xold"))
hp._note_refusal(MID, BORROWER, "panic-underflow", 100.0)
check("одноблочная: протухшая победа — тишина", len(al.sent) == 0)
hp._RL_SINK = None                                      # снять арм за собой

if FAILS:
    print("=== ДРЕЛЬ #123 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ #123 ЗЕЛЁНАЯ: отказ при чужой победе сообщает о себе, канал закрыт без явного арма ===")
