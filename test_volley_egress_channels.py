"""20.08: второй egress-IP — канал рассылки это ПАРА (url, source_ip), а не просто url.

ЗАЧЕМ. Замер с боевой машины: pacer_dry 8758 проб/сутки при transport_reject=0 — связывает НАШ
ЖЕ пейсер на 110/с (замеренная квота приёма секвенсера НА ОДИН IP), а не отказы секвенсера.
Флот удвоен до 220 ног, давление вырастет вдвое в тот же потолок. На Шарлотт заведён второй
адрес на том же интерфейсе net0; замер: connect к mainnet-sequencer 0.0157/0.0146/0.0158с с
нового против 0.0158/0.0149с с основного — канал БЕСПЛАТЕН ПО ВРЕМЕНИ (этим он и отличается от
Вены с RTT 107мс и от прокси).

ГИПОТЕЗА (named limit): вторая корзина существует, ТОЛЬКО если секвенсер лимитирует по IP.
Стенд её не проверяет и проверить не может — это приёмка боем (pacer_dry обязан упасть при том
же боезапасе). Стенд проверяет МЕХАНИКУ: что второй канал действительно отдельный.

Сети здесь нет: подменяется фабрика сессии `_make_session` (шов по заданию) и/или `_seq_session`.

  * ведро ПЕР-КАНАЛЬНОЕ: два канала = вдвое больше отправок до pacer_dry (красный на HEAD);
  * spill с первого канала на второй при сухом ведре первого;
  * сессии ТЁПЛЫЕ: фабрика зовётся на warm_broadcast и НЕ зовётся на отправку;
  * привязка к адресу передаётся в фабрику ровно та, что в канале;
  * непривязываемый адрес выпадает на старте, отправка уходит оставшимся каналом;
  * все адреса мертвы -> рассылка БЕЗ привязки (незнание не закрывает основной путь);
  * пустая VOLLEY_EGRESS_IPS -> поведение БИТ-В-БИТ (ключи вёдер/счётчиков == url, фабрика
    зовётся без привязки, шов _seq_session зовётся ОДНИМ аргументом);
  * боевая tx мимо вёдер и мимо привязки.
"""
import sys
import threading
import time
import types

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.execute as ex  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:220])))
    if not ok:
        FAILS.append(name)


U1 = "https://mainnet-sequencer.base.org"
U2 = "https://backup.example"
IP_A = "185.173.146.134"          # основной адрес Шарлотт
IP_B = "193.233.130.251"          # новый
RAW = "0xdeadbeef"
ADDR = "0x" + "a1" * 20

# ---- швы стенда --------------------------------------------------------------------------
MADE = []          # (url, ip) каждой СОЗДАННОЙ сессии — доказательство «тёплые, не на отправку»
POSTED = []        # (url, ip) каждой фактической отправки


class _FakeSession:
    def __init__(self, url, ip):
        self.url, self.ip = url, ip

    def post(self, url, json=None, timeout=None):
        POSTED.append((url, self.ip))
        return types.SimpleNamespace(json=lambda: {"result": "0xok"})


def fake_make_session(url, ip=None):
    MADE.append((url, ip))
    return _FakeSession(url, ip)


def reset(ips=(), bindable=None):
    ex.VOLLEY_EGRESS_IPS = list(ips)
    ex._CHANNELS_CACHE.clear()
    ex._SEQ_SESSIONS.clear()
    for d in ("_PACER_BUCKETS", "_PACER_DRY", "_PACER_SPILL_SENT", "_PACER_TRANSPORT_REJECT"):
        getattr(ex, d).clear()
    MADE.clear()
    POSTED.clear()
    ex.egress_bindable = (lambda ip: True) if bindable is None else bindable


def arm(addr=ADDR, nonce=500):
    ex._VOLLEY_GAS_USD = 0.0
    ex._VOLLEY_GAS_RESERVED = 0.0
    ex._INFLIGHT = 0
    ex._VOLLEY_LOCKS[addr] = threading.Lock()
    h = "0xprobe%d" % nonce
    ex._VOLLEY_LADDER[addr] = {"q": [(nonce, RAW, h)], "gas": 21000,
                               "max_fee": 10 ** 9, "ts": time.monotonic()}
    ex._VOLLEY_BAL[addr] = [10 ** 20, time.monotonic()]
    ex._VOLLEY_NONCE[addr] = nonce
    return h


def send_paced(url, ip, rate=0.0, burst=1):
    """Отправка через ведро, СОВМЕСТИМАЯ С БАЗОЙ: source_ip передаётся, только если он вообще
    поддерживается. Нужна, чтобы центральное утверждение («две корзины = вдвое больше
    отправок») было ПОВЕДЕНЧЕСКИМ и на HEAD, а не падало трейсбеком про сигнатуру: на HEAD оба
    адреса делят одно ведро, и вторая отправка обязана уйти в pacer_dry."""
    import inspect
    kw = dict(paced=True, rate=rate, burst=burst)
    try:
        if "source_ip" in inspect.signature(ex._post_raw_sync).parameters:
            kw["source_ip"] = ip
    except (TypeError, ValueError):
        pass
    return ex._post_raw_sync(url, RAW, **kw)


# Механизм каналов целиком отсутствует на HEAD — тогда стенд обязан сказать это ОДНОЙ внятной
# строкой и не сыпать трейсбеками из подмены несуществующих швов.
_MISSING = [n for n in ("_make_session", "broadcast_channels", "_chan_key", "egress_bindable",
                        "VOLLEY_EGRESS_IPS", "_CHANNELS_CACHE")
            if not hasattr(ex, n)]
if _MISSING:
    check("механизм каналов рассылки присутствует", False, "нет: " + ", ".join(_MISSING))
    # Центральное утверждение всё равно проверяем — оно поведенческое и работает на HEAD.
    for d in ("_PACER_BUCKETS", "_PACER_DRY", "_PACER_SPILL_SENT"):
        getattr(ex, d, {}).clear()
    _sv = ex._seq_session
    ex._seq_session = lambda url, *a: _FakeSession(url, None)
    try:
        _a1, _ = send_paced(U1, IP_A)
        _a2, _e = send_paced(U1, IP_B)
        check("две корзины = вдвое больше отправок до сухого (2 против 1)",
              _a1 is True and _a2 is True, "второй адрес ушёл в %r: ведро ОБЩЕЕ на url" % (_e,))
    finally:
        ex._seq_session = _sv
    print()
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)

_real_make = ex._make_session
_real_bindable = ex.egress_bindable
_real_ips = list(ex.VOLLEY_EGRESS_IPS)

# =========================================================================================
# 0. НАСТОЯЩАЯ ФАБРИКА — ДО подмены. Всё остальное в этом файле гоняет ФЕЙК, значит боевая
# привязка нигде бы не исполнилась: ошибись мы в имени kwarg'а или в super(), 141 стенд
# остался бы зелёным, а прод молча слал бы с одного адреса. Это ровно «шов подменён выше
# дефекта», и ловится он только исполнением настоящего кода. Сети здесь нет: создание
# сессии и разбор пула — офлайн, connect не делается.
# =========================================================================================
_s_bound = _real_make("https://x.example", "127.0.0.1")
if _s_bound is None:
    check("requests доступен (иначе привязка невозможна в принципе)", False, "нет requests")
else:
    _kw = _s_bound.get_adapter("https://x.example").poolmanager.connection_pool_kw
    check("НАСТОЯЩАЯ фабрика: source_address доехал до PoolManager",
          _kw.get("source_address") == ("127.0.0.1", 0), _kw.get("source_address"))
    check("НАСТОЯЩАЯ фабрика: размеры пула сохранены (32 — пик каскада)",
          _kw.get("maxsize") == 32, _kw.get("maxsize"))
    _kw0 = _real_make("https://x.example").get_adapter("https://x.example") \
        .poolmanager.connection_pool_kw
    check("НАСТОЯЩАЯ фабрика без ip: привязки НЕТ (сегодняшний транспорт не тронут)",
          "source_address" not in _kw0, _kw0.get("source_address"))

ex._make_session = fake_make_session
# Keepalive-тред в стенде не нужен (он бы слал в сеть раз в 15с).
ex._seq_keepalive = lambda url, s, interval=15.0: None

try:
    # =====================================================================================
    # 1. СОСТАВ И ПОРЯДОК КАНАЛОВ
    # =====================================================================================
    reset(ips=[])
    check("пустая ручка: каналы == url без привязок (сегодняшнее поведение)",
          ex.broadcast_channels([U1, U2]) == [(U1, None), (U2, None)],
          ex.broadcast_channels([U1, U2]))
    check("пустая ручка: ключ канала == url посимвольно (ключи вёдер не переименованы)",
          ex._chan_key(U1, None) == U1 and ex._chan_key(U1) == U1)

    reset(ips=[IP_A, IP_B])
    _ch = ex.broadcast_channels([U1, U2])
    check("ручка задана: первичный url размножен по адресам, прочие url — как были",
          _ch == [(U1, IP_A), (U1, IP_B), (U2, None)], _ch)
    check("имя канала несёт IP (это не секрет, в отличие от ключа в пути URL)",
          ex._chan_key(U1, IP_B) == U1 + "|" + IP_B)

    # =====================================================================================
    # 2. ВЕДРО ПЕР-КАНАЛЬНОЕ — суть всей правки (красный на HEAD)
    # =====================================================================================
    # Ведро: rate=0 (не пополняется), burst=1 -> ровно ОДНА отправка на канал.
    reset(ips=[IP_A, IP_B])
    _ok1, _ = send_paced(U1, IP_A)
    _ok2, _e2 = send_paced(U1, IP_A)
    _ok3, _ = send_paced(U1, IP_B)
    check("ведро пер-канальное: тот же url с ДРУГОГО адреса имеет СВОЙ токен",
          _ok1 is True and _ok2 is False and _e2 == "pacer_dry" and _ok3 is True,
          (_ok1, _ok2, _e2, _ok3))
    check("две корзины = вдвое больше отправок до сухого (2 против 1)",
          len(POSTED) == 2, POSTED)

    # Тот же сценарий на ОДНОМ канале — контроль: второй уходит в pacer_dry.
    reset(ips=[])
    _p1, _ = send_paced(U1, None)
    _p2, _e = send_paced(U1, None)
    check("контроль: один канал -> вторая отправка сухая", _p1 is True and _e == "pacer_dry")

    # =====================================================================================
    # 3. ТЁПЛЫЕ СЕССИИ: фабрика на прогреве, а не на отправке
    # =====================================================================================
    reset(ips=[IP_A, IP_B])
    ex.warm_broadcast([U1, U2])
    _made_at_warm = list(MADE)
    check("прогрев создаёт сессию на КАЖДЫЙ канал",
          _made_at_warm == [(U1, IP_A), (U1, IP_B), (U2, None)], _made_at_warm)
    check("прогрев передаёт в фабрику ИМЕННО адрес канала",
          [ip for _u, ip in _made_at_warm] == [IP_A, IP_B, None], _made_at_warm)
    MADE.clear()
    ex._post_raw_sync(U1, RAW, paced=False, source_ip=IP_A)
    ex._post_raw_sync(U1, RAW, paced=False, source_ip=IP_B)
    check("ОТПРАВКА не создаёт сессий (иначе холодный коннект в горячем окне)",
          MADE == [], MADE)
    check("отправка ушла с обоих адресов", POSTED == [(U1, IP_A), (U1, IP_B)], POSTED)

    # =====================================================================================
    # 4. SPILL С ПЕРВОГО КАНАЛА НА ВТОРОЙ ПРИ СУХОМ ВЕДРЕ
    # =====================================================================================
    reset(ips=[IP_A, IP_B])
    ex.warm_broadcast([U1])
    MADE.clear()
    # Выпиваем ведро канала A досуха, ведро B нетронуто.
    ex._PACER_BUCKETS[ex._chan_key(U1, IP_A)] = [0.0, time.monotonic()]
    _h = arm()
    _rpc = types.SimpleNamespace(_web3=lambda: types.SimpleNamespace(
        eth=types.SimpleNamespace(send_raw_transaction=lambda r: None)))
    _cfg = types.SimpleNamespace(tip_gwei=0.05)
    _res = ex.send_presigned_probe(_rpc, _cfg, ADDR, broadcast_raw_urls=[U1])
    _dry, _spill, _rej = ex.volley_pacer_stats()
    check("spill: сухой первый канал -> проба ушла ВТОРЫМ адресом",
          _res == _h and POSTED == [(U1, IP_B)], (_res, POSTED))
    check("spill: pacer_dry записан НА КАНАЛ A (с IP в имени)",
          _dry.get(ex._chan_key(U1, IP_A)) == 1 and not _dry.get(U1), _dry)
    check("spill: spill_sent записан НА КАНАЛ B",
          _spill.get(ex._chan_key(U1, IP_B)) == 1, _spill)
    check("spill: transport_reject пуст (сети не касались)", not _rej, _rej)

    # =====================================================================================
    # 5. ГАРД ПРИВЯЗКИ
    # =====================================================================================
    reset(ips=[IP_A, IP_B], bindable=lambda ip: ip != IP_A)      # адрес A снят с интерфейса
    _ch = ex.broadcast_channels([U1])
    check("непривязываемый адрес ВЫПАДАЕТ из списка каналов", _ch == [(U1, IP_B)], _ch)
    ex.warm_broadcast([U1])
    MADE.clear()
    _h = arm(nonce=600)
    _res = ex.send_presigned_probe(_rpc, _cfg, ADDR, broadcast_raw_urls=[U1])
    check("отправка ушла ОСТАВШИМСЯ каналом (падение привязки не роняет рассылку)",
          _res == _h and POSTED == [(U1, IP_B)], (_res, POSTED))

    reset(ips=[IP_A, IP_B], bindable=lambda ip: False)            # оба мертвы
    _ch = ex.broadcast_channels([U1])
    check("все адреса мертвы -> рассылка БЕЗ привязки (незнание не закрывает основной путь)",
          _ch == [(U1, None)], _ch)
    ex.warm_broadcast([U1])
    _h = arm(nonce=610)
    _res = ex.send_presigned_probe(_rpc, _cfg, ADDR, broadcast_raw_urls=[U1])
    check("и она проходит по основному пути", _res == _h and POSTED == [(U1, None)],
          (_res, POSTED))

    # bind-проба — локальная, сети не касается; настоящая функция на заведомо чужом адресе
    ex.egress_bindable = _real_bindable
    check("egress_bindable: чужой адрес не привязывается (настоящая проба, без сети)",
          ex.egress_bindable("203.0.113.7") is False)

    # =====================================================================================
    # 6. ПУСТАЯ РУЧКА = БИТ-В-БИТ (регрессия против HEAD)
    # =====================================================================================
    reset(ips=[])
    ex.warm_broadcast([U1, U2])
    check("пустая ручка: сессии строятся БЕЗ привязки, по одной на url",
          MADE == [(U1, None), (U2, None)], MADE)
    MADE.clear()
    _h = arm(nonce=700)
    _res = ex.send_presigned_probe(_rpc, _cfg, ADDR, broadcast_raw_urls=[U1, U2])
    _dry, _spill, _rej = ex.volley_pacer_stats()
    check("пустая ручка: одна отправка по первому url, без привязки",
          _res == _h and POSTED == [(U1, None)], (_res, POSTED))
    check("пустая ручка: ключи счётчиков — голые url (вёдра не поделены)",
          all("|" not in k for k in list(_dry) + list(_spill) + list(_rej)),
          (_dry, _spill, _rej))

    # Шов _seq_session на непривязанном канале обязан зваться ОДНИМ аргументом: стенды
    # (test_volley_pacer_spill) подменяют его лямбдой от одного url.
    reset(ips=[])
    _seen_args = []
    _sv_seq = ex._seq_session
    ex._seq_session = lambda url: (_seen_args.append(1), _FakeSession(url, None))[1]
    try:
        ex._post_raw_sync(U1, RAW, paced=False)
        check("непривязанный канал зовёт _seq_session ОДНИМ аргументом (шов стендов цел)",
              _seen_args == [1], _seen_args)
    finally:
        ex._seq_session = _sv_seq

    # =====================================================================================
    # 7. БОЕВАЯ tx — мимо вёдер и мимо привязки
    # =====================================================================================
    reset(ips=[IP_A, IP_B])
    ex.warm_broadcast([U1])
    MADE.clear()
    POSTED.clear()
    _seen_sync = []
    _sv_sync = ex._post_raw_sync
    ex._post_raw_sync = lambda *a, **k: (_seen_sync.append(a), (True, None))[1]
    try:
        ex._post_raw_tx(U1, RAW)
        check("боевая tx не входит в пейсер-точку _post_raw_sync", _seen_sync == [], _seen_sync)
    finally:
        ex._post_raw_sync = _sv_sync
    _dry, _spill, _rej = ex.volley_pacer_stats()
    check("боевая tx не двигает счётчики каналов", not _dry and not _spill and not _rej,
          (_dry, _spill, _rej))
    check("боевая tx идёт НЕПРИВЯЗАННОЙ сессией (её судьба не зависит от нового канала)",
          POSTED == [(U1, None)], POSTED)
finally:
    ex._make_session = _real_make
    ex.egress_bindable = _real_bindable
    ex.VOLLEY_EGRESS_IPS = _real_ips
    ex._CHANNELS_CACHE.clear()
    ex._SEQ_SESSIONS.clear()

print()
if FAILS:
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("egress-каналы: все проверки прошли")
