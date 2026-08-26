"""КЛАССИФИКАТОР БУТА cu_quota_watch (репро крон-сессии 26.08, окно 15:07 по B#5735).

Дефект: бут опознавался ОДНОЙ координатой — молодым скольз-спаном (<16 мин). При 3
рестартах в часе спан сбрасывается постоянно ⇒ ВЕСЬ час помечен бутом (n=11, boot=11,
cu_boot==cu), ex-boot-диагностика молчит В СТОРОНУ УСПОКОЕНИЯ при непрерывном боевом
горении. Молодость процесса не делает боевой огонь прогревом.

Фикс: бут = ДВЕ координаты — молодой спан И прогревный топ-потребитель (_BOOT_TOPS).

Приёмка:
(а) РЕПРО: окно с молодым спаном и боевым топом (send_tx/_receipt) — НЕ бут, идёт в projs;
(б) настоящий прогрев (молодой спан + presign_ladder/bal_warm) — бут, как раньше;
(в) прогревный потребитель на ЗРЕЛОМ спане — фон, не бут;
(г) интеграл ("cu") не зависит от разметки — провайдер списывает всё;
(д) КРАСНЫЙ-НА-БАЗЕ: прежнее правило (одна координата) на репро красит бутом всё.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


from analysis import cu_quota_watch as cw  # noqa: E402


def line(cu, span, top, hh="15", mm="07"):
    return ("2026-08-26 %s:%s:00,000 INFO CU base-mainnet.g.alchemy.com#5735: %d CU/окно = 9 CU/s "
            "(проекция 85.3M/мес, скольз.%dмин 665.9M/мес) | %s" % (hh, mm, cu, span, top))


# ---- (а) РЕПРО 15:07: 11 окон, спан молодой (рестарты 14:08/14:48), топ БОЕВОЙ ----
combat_tops = ["eth_sendRawTransaction@execute.py:send_tx",
               "eth_getTransactionReceipt@spec_fire_live.py:_receipt×497",
               "eth_call@hotpath.py:read_positions×2"]
text = "\n".join(line(60000, 3 + i, combat_tops[i % 3]) for i in range(11))
d = cw.parse_cu_windows(text)["base-mainnet.g.alchemy.com#5735"]
check("(а) РЕПРО: 11 окон боевого топа на молодом спане -> boot=0, ВСЕ в projs",
      d["boot"] == 0 and d["cu_boot"] == 0 and len(d["projs"]) == 11 and d["n_all"] == 11,
      (d["boot"], len(d["projs"])))

# ---- (б) настоящий прогрев остаётся бутом ----
warm = "\n".join([line(9000, 2, "eth_getBlockByNumber@execute.py:presign_ladder×40"),
                  line(5000, 4, "eth_getBalance@execute.py:volley_bal_warm"),
                  line(3000, 6, "eth_getTransactionCount@execute.py:_pending_nonce×9")])
d2 = cw.parse_cu_windows(warm)["base-mainnet.g.alchemy.com#5735"]
check("(б) прогревные топы на молодом спане -> бут, как раньше (boot=3)",
      d2["boot"] == 3 and d2["cu_boot"] == 17000 and len(d2["projs"]) == 0,
      (d2["boot"], d2["cu_boot"]))

# ---- (в) прогревный потребитель на зрелом спане — фон ----
d3 = cw.parse_cu_windows(line(4000, 42, "eth_getBalance@execute.py:volley_bal_warm"))[
    "base-mainnet.g.alchemy.com#5735"]
check("(в) bal_warm на зрелом спане (42мин) -> НЕ бут (фон, идёт в projs)",
      d3["boot"] == 0 and len(d3["projs"]) == 1, d3)

# ---- (г) интеграл не зависит от разметки ----
check("(г) 'cu' считает ВСЁ (провайдер списывает и бут): репро cu=660000",
      d["cu"] == 660000, d["cu"])

# ---- (д) КРАСНЫЙ-НА-БАЗЕ: одна координата красит репро бутом целиком ----
def _legacy_boot(span_min):
    return span_min < cw.BOOT_SPAN_MIN


_legacy_all_boot = all(_legacy_boot(3 + i) for i in range(11))
check("(д) КРАСНЫЙ-НА-БАЗЕ: прежнее правило (только спан) метит бутом все 11 окон репро",
      _legacy_all_boot and d["boot"] == 0,
      "старое правило не воспроизводит дефект")

# ---- гарды дрейфа ----
_S = open("analysis/cu_quota_watch.py").read()
check("две координаты записаны в коде (спан И топ)",
      "_BOOT_TOPS.search(top" in _S and "span_min < BOOT_SPAN_MIN and " in _S)
check("причина записана (репро 15:07, ложь в сторону успокоения)",
      "15:07" in _S and "УСПОКОЕНИ" in _S)
check("боевые потребители НЕ входят в прогревный список",
      not cw._BOOT_TOPS.search("eth_sendRawTransaction@execute.py:send_tx")
      and not cw._BOOT_TOPS.search("eth_getTransactionReceipt@spec_fire_live.py:_receipt")
      and not cw._BOOT_TOPS.search("eth_call@hotpath.py:read_positions"))

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: бут = молодой спан И прогревный топ; боевой огонь не красится "
                     "прогревом на многорестартном часе"))
sys.exit(1 if FAILS else 0)
