"""Юниты ротации edge-IP предикт-фида (29.07: два из восьми CDN-edge vision гнилые — залипание
12с/keepalive 1011, DNS-рулетка ловила их ~25% реконнектов). Без сети: резолв замокан."""
import sys
sys.path.insert(0, ".")
import chain.pricefeed as pf

IPS = ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def _with_resolve(ips):
    pf._resolve_edges_orig = getattr(pf, "_resolve_edges_orig", pf._resolve_edges)
    pf._resolve_edges = lambda host: list(ips)


# 1) карантин: гнилой IP не выбирается, пока не истёк срок
pf._edge_bad.clear()
_with_resolve(IPS)
t = [1000.0]
now = lambda: t[0]
pf._edge_bad["2.2.2.2"] = 900.0                      # упал 100с назад (карантин 600с)
got = {pf._pick_edge("h", now=now) for _ in range(50)}
assert got == {"1.1.1.1", "3.3.3.3"}, got
t[0] = 900.0 + pf.EDGE_QUARANTINE_SEC + 1
got = {pf._pick_edge("h", now=now) for _ in range(80)}
assert "2.2.2.2" in got, "истёкший карантин обязан вернуть IP в ротацию"
print("UNIT_OK: карантин исключает гнилой IP и отпускает по истечении")

# 2) все в карантине -> карантин обнуляется, выбор из всех (хуже DNS-рулетки не станет)
pf._edge_bad.clear()
for ip in IPS:
    pf._edge_bad[ip] = t[0]
got = pf._pick_edge("h", now=now)
assert got in IPS and pf._edge_bad == {}, (got, pf._edge_bad)
print("UNIT_OK: тотальный карантин самообнуляется")

# 3) резолв пуст -> None (фолбэк на коннект по hostname)
_with_resolve([])
assert pf._pick_edge("h", now=now) is None
print("UNIT_OK: пустой резолв -> коннект по hostname (прежнее поведение)")

# 4) _run маркирует упавший edge: connect ставит _edge_last и кидает -> IP в карантине
pf._edge_bad.clear()
pf._edge_last["ip"] = None


def _boom():
    pf._edge_last["ip"] = "9.9.9.9"
    raise ConnectionError("edge down")


stopper = {"n": 0}


def _sleep(_s):
    stopper["n"] += 1
    if stopper["n"] >= 1:
        feed._stop.set()


feed = pf.PriceFeed(["BTCUSDT"], connect=_boom, sleep=_sleep)
feed._run()
assert "9.9.9.9" in pf._edge_bad, "упавший edge обязан попасть в карантин"
print("UNIT_OK: обрыв в _run отправляет edge в карантин")

# 5) кастомный connect без ротации (тесты/инжект) — no-op, ничего не карантинится лишний раз
pf._edge_bad.clear()
pf._edge_last["ip"] = None
stopper["n"] = 0
feed2 = pf.PriceFeed(["BTCUSDT"], connect=lambda: (_ for _ in ()).throw(OSError("x")), sleep=_sleep)
feed2._stop.clear()


def _sleep2(_s):
    feed2._stop.set()


feed2._sleep = _sleep2
feed2._run()
assert pf._edge_bad == {}, pf._edge_bad
print("UNIT_OK: без ротации маркировка — no-op")

pf._resolve_edges = pf._resolve_edges_orig
print("=== ВСЕ EDGE-РОТАЦИЯ ЮНИТЫ ПРОШЛИ ===")
