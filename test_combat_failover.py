"""Юниты failover боевого READ-пути (подпись 02.08): атомарный указатель, гистерезис
жёстких отказов, head-guard, липкий пин (назад не посреди окна), bg_shed, откат."""
import types

import chain.failover as fo
from chain.failover import FailoverRpc


class FakeProvider:
    """Утиный BaseRpc: rpc_url + _web3().eth.block_number (int или Exception)."""

    def __init__(self, url, head=100):
        self.rpc_url = url
        self.chain_id = 8453
        self.request_timeout = 5
        self.head = head          # int -> отдать; Exception -> бросить

    def _web3(self):
        p = self

        class _Eth:
            @property
            def block_number(self):
                if isinstance(p.head, Exception):
                    raise p.head
                return int(p.head)

        return types.SimpleNamespace(eth=_Eth())


class FakeAlerter:
    def __init__(self):
        self.sent = []

    def send_async(self, text):
        self.sent.append(text)


def mk(bg_url="https://bg.example/key", now=None):
    prim = FakeProvider("https://n2.example/keyA", head=100)
    alt1 = FakeProvider("https://n1.example/keyB", head=100)
    alt2 = FakeProvider("https://bg.example/key", head=100)
    al = FakeAlerter()
    clock = {"t": 1000.0}
    f = FailoverRpc(prim, [alt1, alt2], bg_url=bg_url, alerter=al,
                    now_fn=lambda: clock["t"])
    return f, (prim, alt1, alt2), al, clock


# 1) Делегация: горячий путь читает указатель, никакого health inline
f, (p0, p1, p2), al, clk = mk()
assert f.rpc_url == p0.rpc_url and f._web3() is not None
assert f.stats["checks"] == 0, "конструктор/делегация не смеют дёргать health"
print("UNIT_OK: делегация указателя без лишних вызовов")

# 2) Гистерезис: 2 отказа держат primary, 3-й — свитч (head-guard кандидата ок)
fo.COMBAT_FAILOVER = True
p0.head = ConnectionError("conn refused")
f._check_once(); f._check_once()
assert f._idx == 0, "до порога гистерезиса свитча нет"
f._check_once()
assert f._idx == 1 and f.rpc_url == p1.rpc_url, "3-й жёсткий отказ -> кандидат №1"
assert f.stats["switches"] == 1 and al.sent and "COMBAT-FAILOVER" in al.sent[0]
print("UNIT_OK: гистерезис 3 жёстких отказов + TG-строка")

# 3) Head-guard: отстающий кандидат пропускается, свежий берётся
f2, (q0, q1, q2), al2, clk2 = mk()
f2._last_head = 200
q0.head = TimeoutError("t/o")
q1.head = 150                     # отстаёт от виденного нами 200 -> пропуск
q2.head = 205
for _ in range(3):
    f2._check_once()
assert f2._idx == 2 and f2.rpc_url == q2.rpc_url, "head-guard обязан выкинуть отстающего"
print("UNIT_OK: head-guard — голова кандидата покрывает виденное")

# 4) bg_shed: указатель на URL bg-пути => фон уступает (и синглтон-обёртка)
assert f2._bg_collision() is True
fo._ACTIVE = f2
assert fo.bg_shed() is True
fo._ACTIVE = f
assert fo.bg_shed() is False      # f стоит на №1, это не bg-URL
print("UNIT_OK: bg_shed при коллизии ключа, чист на чужом")

# 5) Липкий пин: primary здоров, но окно активно — назад не ходим; окно закрылось + дожив — назад
p0.head = 300
f._windows_probe = lambda: 1
f._check_once()                   # первый здоровый тик primary: ok_since стартует
clk["t"] += 120.0                 # дожив больше порога, но окно живо
f._check_once()
assert f._idx == 1, "назад посреди окна нельзя"
f._windows_probe = lambda: 0
f._check_once()
assert f._idx == 0, "окон 0 + primary здоров >= порога -> возврат"
assert f.stats["switches"] == 2
print("UNIT_OK: пин липкий — возврат только вне окна после доживания")

# 6) Возврат сбрасывается, если primary опять флапнул до порога доживания
f._switch(1, "тест")
p0.head = ValueError("boom")
f._check_once()
assert f._primary_ok_since is None
p0.head = 400
f._check_once()                   # ok_since заново
clk["t"] += 10.0
f._check_once()
assert f._idx == 1, "доживание не накоплено — рано"
print("UNIT_OK: флап primary обнуляет доживание")

# 7) Откат COMBAT_FAILOVER=0: отказы копятся, свитча нет
fo.COMBAT_FAILOVER = False
f3, (r0, r1, r2), al3, _ = mk()
r0.head = ConnectionError("dead")
for _ in range(5):
    f3._check_once()
assert f3._idx == 0 and f3.stats["switches"] == 0, "откат обязан прибить указатель"
fo.COMBAT_FAILOVER = True
print("UNIT_OK: откат COMBAT_FAILOVER=0 — указатель прибит")

# 8) build_failover: без URLS возвращает primary как был
_orig_urls = fo.COMBAT_FAILOVER_URLS
fo.COMBAT_FAILOVER_URLS = []
prim = FakeProvider("https://n2.example/keyA")
assert fo.build_failover(prim, types.SimpleNamespace(bg_rpc_url="")) is prim
fo.COMBAT_FAILOVER_URLS = _orig_urls
print("UNIT_OK: build_failover без кандидатов — прежний rpc")

print("=== ВСЕ COMBAT-FAILOVER ЮНИТЫ ПРОШЛИ ===")
