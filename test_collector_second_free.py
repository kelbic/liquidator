"""22.08 (kelbic, ключ D): второй БЕСПЛАТНЫЙ узел квитанций перед платным откатом.

Страховка боевым = ~447 партий/сутки на платный ключ. Цепочка: свой bg (dRPC, chunk 3) ->
D (Alchemy free) -> платный. Гарды: спасённая D партия платный НЕ трогает; отказ обоих
уходит боевым с СОСТАВНЫМ why; URL_2 пуст -> поведение бит-в-бит прежнее (откат = снять
звено, не параметр).
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402
from chain.execute import ReceiptsUnavailable  # noqa: E402

HS = ["0xaa", "0xbb"]
CALLS = []


def mk(fail=(), empty=()):
    def bf(url, hs, chunk=0):
        CALLS.append((url, chunk))
        if url in fail:
            raise ReceiptsUnavailable("узел %s лёг" % url)
        return {h: (None if url in empty else {"status": "0x1"}) for h in hs}
    return bf


FB, SEC = [], []

# ---- 1. Выделенный жив: ни D, ни боевой не трогаются ----
CALLS.clear()
out = sfl.collect_receipts(HS, "u1", "combat", mk(), on_fallback=FB.append,
                           dedicated_chunk=3, second_url="uD", on_second=SEC.append)
check("выделенный жив: один вызов, chunk=3", CALLS == [("u1", 3)], CALLS)
check("страховки не звались", not FB and not SEC)

# ---- 2. Выделенный лёг -> партию спасает D, платный НЕ тронут ----
CALLS.clear()
out = sfl.collect_receipts(HS, "u1", "combat", mk(fail=("u1",)), on_fallback=FB.append,
                           dedicated_chunk=3, second_url="uD", on_second=SEC.append)
check("D спас партию: цепочка u1->uD, боевого НЕТ",
      CALLS == [("u1", 3), ("uD", 0)] and out["0xaa"]["status"] == "0x1", CALLS)
check("on_second зван с причиной, on_fallback молчит",
      len(SEC) == 1 and "отказ узла (" in SEC[0] and "узел u1 лёг" in SEC[0] and not FB,
      (SEC, FB))

# ---- 3. Легли ОБА бесплатных -> боевой, why составной ----
CALLS.clear()
SEC.clear()
out = sfl.collect_receipts(HS, "u1", "combat", mk(fail=("u1", "uD")), on_fallback=FB.append,
                           dedicated_chunk=3, second_url="uD", second_chunk=5,
                           on_second=SEC.append)
check("оба легли: u1(3) -> uD(5) -> combat",
      CALLS == [("u1", 3), ("uD", 5), ("combat", 0)], CALLS)
check("why составной (оба отказа названы)",
      len(FB) == 1 and "узел u1 лёг" in FB[0] and "отказ узла-2" in FB[0]
      and "узел uD лёг" in FB[0] and not SEC, FB)

# ---- 4. НЕГАТИВНЫЙ КОНТРОЛЬ: URL_2 пуст = бит-в-бит прежнее поведение ----
CALLS.clear()
FB.clear()
sfl.collect_receipts(HS, "u1", "combat", mk(fail=("u1",)), on_fallback=FB.append,
                     dedicated_chunk=3)
check("без URL_2: u1 -> combat, why прежней формы",
      CALLS == [("u1", 3), ("combat", 0)] and len(FB) == 1
      and FB[0].startswith("отказ узла (") and "узел u1 лёг" in FB[0], (CALLS, FB))
CALLS.clear()
sfl.collect_receipts(HS, "", "combat", mk())
check("без обоих бесплатных: сразу боевой", CALLS == [("combat", 0)], CALLS)

# ---- 5. Выделенного нет, D есть: D первым, спасение НЕ считается (нечего спасать) ----
CALLS.clear()
FB.clear()
SEC.clear()
sfl.collect_receipts(HS, "", "combat", mk(), second_url="uD", on_second=SEC.append,
                     on_fallback=FB.append)
check("только D: один вызов uD, счётчики молчат",
      CALLS == [("uD", 0)] and not SEC and not FB, (CALLS, SEC, FB))

# ---- 6. Режим FALLBACK_ON_EMPTY: пустая партия у D тоже гонит дальше ----
_sv = sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY
sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY = True
CALLS.clear()
FB.clear()
sfl.collect_receipts(HS, "u1", "combat", mk(empty=("u1", "uD")), on_fallback=FB.append,
                     second_url="uD")
check("empty-режим: обе пустые партии -> боевой, why откат-2",
      CALLS == [("u1", 0), ("uD", 0), ("combat", 0)] and "откат-2" in FB[0], (CALLS, FB))
sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY = _sv

# ---- 7. Гарды дрейфа: проводка в боевом шве, конфиг из ALCHEMY_D_URL ----
_SRC = open("chain/spec_fire_live.py").read()
check("шов _receipts_fn передаёт second_url/chunk_2/on_second",
      "second_url=SPEC_FIRE_COLLECTOR_RPC_URL_2" in _SRC
      and "second_chunk=SPEC_FIRE_COLLECTOR_BATCH_CHUNK_2" in _SRC
      and "on_second=self._note_collector_second" in _SRC)
check("URL_2 берётся из ALCHEMY_D_URL (ключ только в .env)",
      'os.environ.get("ALCHEMY_D_URL", "")' in _SRC)
check("учёт спасений — сбоку, лог с прореживанием",
      "collector_second" in _SRC and "_collector_2_logged" in _SRC)
check("узел-судья эффекта назван (CU-атрибуция, не свой счётчик)",
      "свой счётчик слеп" in _SRC)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: цепочка квитанций free->D->платный; URL_2 пуст = прежнее бит-в-бит"))
sys.exit(1 if FAILS else 0)
