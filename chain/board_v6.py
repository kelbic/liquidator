"""Обвязка боевого мультислота v6-D4 (задача #129, ТЗ docs/SPEC_V6D4_WIRING.md).

ОТДЕЛЬНЫЙ модуль, ВНЕ боевых путей v5. v5-тракт (chain/execute.py:encode_set_plan / send_board_tx
и одностраничная доска) остаётся в бою БИТ-В-БИТ до отдельного переключения (§0). Здесь — только
кодирование owner-функций v6, приёмка арма ПО ЦЕПИ (§3), указатель с `until` С ЦЕПИ (§4),
раскладка §2 чистыми функциями и мелочи §6/§7/§9. Деплой/переключение/переподпись лестниц — НЕ
здесь (работа сессии).

Каноны, по которым построен модуль:
 * «ХЭШ TX — НЕ ЭФФЕКТ» (#84): арм признаётся севшим ТОЛЬКО по двум доказательствам с цепи —
   квитанция НАШЕГО хэша status==1 И совпадение groupInfo(deadline,n)+targetInfo(borrower) по
   КАЖДОЙ цели. Сверки одного borrower мало (кит держится в топе часами — переарм по той же цели).
 * ЛИМИТ ГАЗА АРМА — от ЗАМЕРЕННОГО (12 757 108 на 8 целей/2КБ) с запасом, не от лимита v5:
   старая формула v5 (120k+22·байт) была МЕНЬШЕ стоимости записи и арм не садился НИ РАЗУ.
 * «UNTIL С ЦЕПИ» (§4): TTL указателя = min(groupInfo.deadline по спану), ПРОЧИТАННЫЙ с цепи, а
   не из настенных часов — иначе setHot законно откатывает (gr.deadline >= until).
 * «ГРОМКИЙ ОТКАЗ» (§9): группа собирается только из целей с непересекающимися пулами; пересечение
   — не тихое наследование v5-minOut, а исключение при сборке.

I/O ИНЖЕКТИРУЕТСЯ: функции цепи берут `call_fn(to, data)->bytes` и `send_fn(calldata,gas)->receipt`
параметрами. Это делает приёмку §3/§4 тестируемой без форка и разводит логику с транспортом
(транспорт — send_board_tx образца v5; проводку делает сессия при переключении).
"""
import time
import os
from eth_abi import encode as _abi_encode
from eth_abi import decode as _abi_decode
from eth_utils import keccak

ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# --- канонические сигнатуры (селекторы сверены с contracts/out/.../LiquidatorV6.json) ----------
_MP_T = "(address,address,address,address,uint256)"
# struct Target: borrower, repaidShares, seizedAssets, swapTarget, swapCallData, minProfit,
#                unwrapTarget, triggerPrice  (порядок ПОДТВЕРЖДЁН артефактом и test_fork_v6d4.target)
_TARGET_T = "(address,uint128,uint128,address,bytes,uint256,address,uint256)"

_SIG = {
    "setGroup": "setGroup(uint256,%s,uint64,uint256,%s[])" % (_MP_T, _TARGET_T),
    "setTargets": "setTargets(uint256,uint8,%s[])" % _TARGET_T,
    "setHot": "setHot(uint8,uint8,uint64,bytes32)",
    "clearHot": "clearHot()",
    "clearGroup": "clearGroup(uint256)",
    "clearTarget": "clearTarget(uint256,uint256)",
    "setAuthorizedBatch": "setAuthorizedBatch(address[],bool)",
    "groupInfo": "groupInfo(uint256)",
    "targetInfo": "targetInfo(uint256,uint256)",
    "hotInfo": "hotInfo()",
}
# Эталон селекторов (methodIdentifiers артефакта). Модуль сверяет их при импорте: расхождение =
# сигнатура разошлась с ABI и кодирование молча ушло бы в чужой селектор (класс #84).
_SELECTOR_REF = {
    "setGroup": "7a193c68", "setTargets": "60dcac36", "setHot": "ca552e20",
    "clearHot": "afce807b", "clearGroup": "a3553332", "clearTarget": "4c5e1866",
    "setAuthorizedBatch": "35474cd2", "groupInfo": "03d480f3", "targetInfo": "6438be07",
    "hotInfo": "a87531a8",
}


def selector(name: str) -> bytes:
    return keccak(text=_SIG[name])[:4]


def _selftest_selectors():
    bad = {n: selector(n).hex() for n in _SIG if selector(n).hex() != _SELECTOR_REF[n]}
    if bad:
        raise RuntimeError("СЕЛЕКТОР РАЗОШЁЛСЯ С ABI (кодирование ушло бы в чужую функцию): %s" % bad)


_selftest_selectors()


# ==================================================================================================
# ФЛАГ БОЕВОЙ ПРОВОДКИ (#135). ДЕФОЛТ 0 = v6 НЕ в бою: тракт v5 работает БИТ-В-БИТ.
# ==================================================================================================
def _env_on(name: str) -> bool:
    """БЕЛЫЙ СПИСОК, а не чёрный. Прежняя форма (`... not in ("0","false","no")`) включала
    механизм на ПУСТОЙ переменной (`SPEC_FIRE_V6=` в .env) — для выключателя это безопасно
    (fail-open к прежнему поведению), для ВКЛЮЧАТЕЛЯ боевого тракта нет: опечатка в .env
    переводила бы флот на другой контракт молча. Незнание обязано закрывать гард."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Единственный переключатель проводки. Читается ДИНАМИЧЕСКИ (через атрибут модуля), чтобы
# стенд мог поставить ОБЕ полярности в одном процессе и сравнить их исполнением, а не чтением.
SPEC_FIRE_V6 = _env_on("SPEC_FIRE_V6")
# Индекс группы, которую занимает боевая доска (§2: дефолт count=1 — вся плотность на один
# рынок, пока звено Г не решено). Ручка на случай, если группа 0 занята дрелью/ручной работой.
SPEC_FIRE_V6_GROUP = int(os.environ.get("SPEC_FIRE_V6_GROUP", "0"))


def v6_enabled() -> bool:
    """ЕДИНСТВЕННЫЙ вопрос «идём ли по v6». Функция, а не голое чтение константы: у флага один
    читатель-источник, и стенду достаточно поставить `board_v6.SPEC_FIRE_V6`."""
    return bool(SPEC_FIRE_V6)


def is_v6_contract(call_fn, addr) -> bool:
    """Отвечает ли `addr` на `hotInfo()` (то есть это v6, а не v5)?

    Гард §0/шага 4: флаг включён, а `LIQUIDATOR_ADDRESS` остался на v5 — тогда setGroup ушёл бы
    в fallback v5 (тихий no-op или реверт), приёмка сказала бы «арм не сел», и никто бы не понял,
    что перепутан АДРЕС. Одно чтение, ответ ГРОМКИЙ у вызывающего.
    v5 на 4-байтовую калдату отвечает ревертом/пустотой -> декодер бросает -> False.
    """
    try:
        read_hot_info(call_fn, addr)
        return True
    except Exception:
        return False


# --- эталон газа арма (§3). Замер: 8 целей, роут 2 КБ => 12 757 108 (с квитанции стенда). -----
ARM_GAS_REF = 12_757_108          # цель = 8, роут 2048 Б
ARM_GAS_SAFETY = float(os.environ.get("V6_ARM_GAS_SAFETY", "1.35"))   # запас к замеренному
_ARM_GAS_FLOOR = int(ARM_GAS_REF * ARM_GAS_SAFETY)   # нижний предел для конфигурации (8, 2КБ)


def _addr(a: str) -> str:
    if isinstance(a, bytes):
        a = "0x" + a.hex()
    return a


def _bytes(x) -> bytes:
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if isinstance(x, str):
        return bytes.fromhex(x[2:] if x.startswith("0x") else x)
    raise TypeError("ожидались bytes/hex, дано %r" % type(x))


def _hexcd(selector_bytes: bytes, types, values) -> str:
    return "0x" + (selector_bytes + _abi_encode(types, values)).hex()


# ==================================================================================================
# §6. ЦЕЛЬ: дефолт repaidShares. seizedAssets — точечно (класс #79), называется явно.
# ==================================================================================================
def make_target(borrower, *, repaid_shares=0, seized_assets=0, swap_target,
                swap_calldata, min_profit=0, unwrap_target=ZERO_ADDR, trigger_price=0):
    """ABI-tuple цели для setGroup/setTargets. §6: ДЕФОЛТ — repaidShares (seizedAssets=0).

    Ровно одно из repaid_shares/seized_assets обязано быть ненулевым (это же требует Morpho — мы
    ловим ошибку ДО арма, а не молча льём нулевую заявку). seized_assets применять там, где
    точность входа свопа важнее инвариантности к падению цены (§6, класс #79) — и называть явно.
    Обрезание uint128 — ГРОМКО (класс #121): значение сверх 2^128-1 = исключение, не тихая маска.
    """
    rs, sa = int(repaid_shares), int(seized_assets)
    if (rs == 0) == (sa == 0):
        raise ValueError("§6/К1: ровно одно из repaidShares/seizedAssets обязано быть ненулевым "
                         "(repaid=%d seized=%d)" % (rs, sa))
    for nm, v in (("repaidShares", rs), ("seizedAssets", sa)):
        if v < 0 or v >= (1 << 128):
            raise ValueError("§6/К1: %s=%d не влезает в uint128 (обрезание запрещено)" % (nm, v))
    if not (0 <= int(trigger_price) < (1 << 256)) or not (0 <= int(min_profit) < (1 << 256)):
        raise ValueError("minProfit/triggerPrice вне uint256")
    return (_addr(borrower), rs, sa, _addr(swap_target), _bytes(swap_calldata),
            int(min_profit), _addr(unwrap_target), int(trigger_price))


def is_repaid_shares_target(t) -> bool:
    """Дефолтная заявка (§6)? repaidShares!=0 и seizedAssets==0."""
    return int(t[1]) != 0 and int(t[2]) == 0


# ==================================================================================================
# ОТПЕЧАТОК РЫНКА (§2.5б/§4). keccak256(abi.encode(MarketParams)) — совпадает с контрактом.
# ==================================================================================================
def mp_tuple(mp) -> tuple:
    """MarketParams -> ABI-tuple. Принимает dict (loanToken,...) или готовый 5-кортеж."""
    if isinstance(mp, (tuple, list)):
        if len(mp) != 5:
            raise ValueError("MarketParams кортеж обязан быть из 5 полей")
        return (_addr(mp[0]), _addr(mp[1]), _addr(mp[2]), _addr(mp[3]), int(mp[4]))
    return (_addr(mp["loanToken"]), _addr(mp["collateralToken"]), _addr(mp["oracle"]),
            _addr(mp["irm"]), int(mp["lltv"]))


def mp_hash(mp) -> bytes:
    """§2.5б: keccak256(abi.encode(mp)). Это же значение setHot требует у каждой группы спана."""
    return keccak(_abi_encode([_MP_T], [mp_tuple(mp)]))


# ==================================================================================================
# КОДИРОВАНИЕ owner-функций (образец — chain/execute.py:encode_set_plan)
# ==================================================================================================
def encode_set_group(g, mp, deadline, gate_trigger, targets) -> str:
    """setGroup(g, mp, deadline, gateTrigger, Target[]). Дорого — делается ЗАРАНЕЕ, в тишине."""
    if not (0 <= int(g) < 8):
        raise ValueError("g вне [0,8)")
    if len(targets) > 8:
        raise ValueError("§1: >8 целей в группе (n<=8)")
    return _hexcd(selector("setGroup"),
                  ["uint256", _MP_T, "uint64", "uint256", "%s[]" % _TARGET_T],
                  [int(g), mp_tuple(mp), int(deadline), int(gate_trigger), list(targets)])


def encode_set_targets(g, from_idx, targets) -> str:
    """setTargets(g, from, Target[]) — докладка каскада БЕЗ заголовка (mp/deadline целы). §2.5а:
    диапазон обязан быть НЕПРЕРЫВНЫМ (from<=n) — это проверяет контракт, здесь только кодирование."""
    if not (0 <= int(g) < 8) or not (0 <= int(from_idx) <= 8):
        raise ValueError("g/from вне диапазона")
    return _hexcd(selector("setTargets"), ["uint256", "uint8", "%s[]" % _TARGET_T],
                  [int(g), int(from_idx), list(targets)])


def encode_set_hot(g0, count, until, mphash) -> str:
    """setHot(g0, count, until, mpHash). `until` брать С ЦЕПИ (§4, until_from_chain);
    mpHash — от армленных параметров (mp_hash). ГОЛЫЙ индекс запрещён каноном §2.5б."""
    mph = mphash if isinstance(mphash, (bytes, bytearray)) else _bytes(mphash)
    if len(mph) != 32:
        raise ValueError("mpHash обязан быть 32 байта")
    if not (0 <= int(g0) < 256 and 0 <= int(count) < 256):
        raise ValueError("g0/count вне uint8")
    return _hexcd(selector("setHot"), ["uint8", "uint8", "uint64", "bytes32"],
                  [int(g0), int(count), int(until), bytes(mph)])


def encode_clear_hot() -> str:
    return "0x" + selector("clearHot").hex()


def encode_clear_group(g) -> str:
    return _hexcd(selector("clearGroup"), ["uint256"], [int(g)])


def encode_clear_target(g, i) -> str:
    """§7: погасить ОДНУ цель после ПОДТВЕРЖДЁННОЙ чужой победы по ней (источник факта —
    подписчик Liquidate фонового индекса #110/#123, НЕ наш журнал; проводку делает сессия)."""
    if not (0 <= int(g) < 8 and 0 <= int(i) < 8):
        raise ValueError("g/i вне [0,8)")
    return _hexcd(selector("clearTarget"), ["uint256", "uint256"], [int(g), int(i)])


def encode_set_authorized_batch(addrs, ok=True) -> str:
    """setAuthorizedBatch: 110 ног авторизуются одной-двумя tx (§3)."""
    return _hexcd(selector("setAuthorizedBatch"), ["address[]", "bool"],
                  [[_addr(a) for a in addrs], bool(ok)])


# --- удобство §1: ПЕРЕД плановой перезарядкой/зачисткой группы спана зовём clearHot -----------
# Это НЕ гард (гард — реверт HotSpanLive в контракте), а вежливость: перезарядка группы живого
# спана иначе законно откатится. Порядок: clearHot -> setGroup(...) -> setHot(...) заново.
def encode_clear_hot_before_rearm() -> str:
    return encode_clear_hot()


# ==================================================================================================
# ЧТЕНИЯ (декодеры ответов groupInfo/targetInfo/hotInfo). Вход — сырые байты eth_call.
# ==================================================================================================
def encode_group_info_call(g) -> str:
    return _hexcd(selector("groupInfo"), ["uint256"], [int(g)])


def encode_target_info_call(g, i) -> str:
    return _hexcd(selector("targetInfo"), ["uint256", "uint256"], [int(g), int(i)])


def encode_hot_info_call() -> str:
    return "0x" + selector("hotInfo").hex()


def decode_group_info(raw):
    """(oracle, deadline, gate, n)."""
    o, dl, gate, n = _abi_decode(["address", "uint64", "uint256", "uint8"], _bytes(raw))
    return _addr(o), int(dl), int(gate), int(n)


def decode_target_info(raw):
    """(borrower, repaidShares, seizedAssets, triggerPrice)."""
    b, rs, sa, tp = _abi_decode(["address", "uint128", "uint128", "uint256"], _bytes(raw))
    return _addr(b), int(rs), int(sa), int(tp)


def decode_hot_info(raw):
    """(g0, count, until)."""
    g0, cnt, until = _abi_decode(["uint8", "uint8", "uint64"], _bytes(raw))
    return int(g0), int(cnt), int(until)


def _addr_eq(a, b) -> bool:
    return _addr(a).lower() == _addr(b).lower()


# --- тонкие обёртки чтения (call_fn(to, data)->bytes инжектируется) ---------------------------
def read_group_info(call_fn, addr, g):
    return decode_group_info(call_fn(addr, encode_group_info_call(g)))


def read_target_info(call_fn, addr, g, i):
    return decode_target_info(call_fn(addr, encode_target_info_call(g, i)))


def read_hot_info(call_fn, addr):
    return decode_hot_info(call_fn(addr, encode_hot_info_call()))


# ==================================================================================================
# §4. УКАЗАТЕЛЬ: until = min(groupInfo.deadline по спану), ПРОЧИТАННЫЙ С ЦЕПИ.
# ==================================================================================================
def until_from_chain(call_fn, addr, g0, count):
    """§4: TTL указателя из того, что в контракте ЛЕЖИТ, а не из наших часов. min по спану.
    Мёртвая группа спана (deadline==0) = арм не сел => наводить нечего (громкий отказ)."""
    if int(count) < 1:
        raise ValueError("count<1: указатель без спана")
    dls = []
    for g in range(int(g0), int(g0) + int(count)):
        if g >= 8:
            raise ValueError("§2.5б: спан вылезает за 8 групп (g0=%d count=%d)" % (g0, count))
        _, dl, _, n = read_group_info(call_fn, addr, g)
        if dl == 0 or n == 0:
            raise ValueError("§4: группа %d спана не армлена (deadline=%d n=%d) — until не из чего "
                             "считать" % (g, dl, n))
        dls.append(dl)
    return min(dls)


def mphash_from_chain_matches(call_fn, addr, g0, count, mp):
    """Все группы спана несут ОДИН рынок (mp)? Сверяем oracle каждой группы с mp (дешёвая
    предпроверка перед setHot; сам отпечаток контракт сверит по keccak в setHot)."""
    want = mp_tuple(mp)[2]  # oracle
    for g in range(int(g0), int(g0) + int(count)):
        oracle, _, _, _ = read_group_info(call_fn, addr, g)
        if not _addr_eq(oracle, want):
            return False
    return True


# ==================================================================================================
# §3. ПРИЁМКА АРМА ПО ЦЕПИ: ОБА доказательства, оба привязаны к НАШЕЙ tx.
# ==================================================================================================
def status_of(receipt) -> int:
    """status квитанции из web3-dict (int) ИЛИ сырого JSON-RPC (hex-строка). None -> 0."""
    if receipt is None:
        return 0
    s = receipt.get("status") if isinstance(receipt, dict) else getattr(receipt, "status", None)
    if s is None:
        return 0
    return int(s, 16) if isinstance(s, str) else int(s)


def _await_head(head_fn, min_block, tries=15, sleep_fn=None):
    """ГАРД ОТСТАВАНИЯ УЗЛА перед чтением состояния (боевой урок 16.08, ДВА ложных красных за
    один прогон деплоя: авторизация 110 ног прочиталась как «107 из 110», а севший `setHot`
    (status=1, газ 61 887) — как «указатель не сел»). Боевой RPC балансируется, и чтение сразу
    за квитанцией попадает на узел, отставший на блок.

    Отрицательный ответ узла, ГОЛОВА которого ниже блока нашей записи, — это НЕЗНАНИЕ, а не
    факт. В бою цена ошибки несимметрична: ложное «арм не сел» гасит огонь на живой доске.
    Возврат: True — голова доросла (читать можно), False — узел так и не догнал.
    """
    if head_fn is None or min_block is None:
        return True
    _sleep = sleep_fn or time.sleep
    for _ in range(int(tries)):
        try:
            if int(head_fn()) >= int(min_block):
                return True
        except Exception:
            pass
        _sleep(1.0)
    return False


def verify_arm(call_fn, addr, g, our_receipt, expected_deadline, borrowers,
               head_fn=None):
    """§3: армленная группа засчитывается ТОЛЬКО если ОБА доказательства с НАШЕЙ tx:
      1) квитанция НАШЕГО хэша status==1;
      2) groupInfo(g).deadline == НАШ deadline И groupInfo(g).n == число отправленных целей,
         плюс targetInfo(g,i).borrower совпадает по КАЖДОЙ цели i<n.
    Сверки одного borrower МАЛО (#84 хвост): дедлайн+полный список ловят и чужой слот, и
    протухший переарм той же цели. Возврат (ok, reason) — reason ГРОМКИЙ при осечке.
    """
    if status_of(our_receipt) != 1:
        return False, "квитанция НАШЕЙ tx status!=1 (арм сгорел/не сел, #84) — доска НЕ armed"
    land = (our_receipt or {}).get("blockNumber")
    if not _await_head(head_fn, land):
        return False, ("узел не дорос до блока записи %s — состояние НЕИЗВЕСТНО (не читаем "
                       "отставший узел как «арм не сел»)" % land)
    oracle, dl, gate, n = read_group_info(call_fn, addr, g)
    if int(dl) != int(expected_deadline):
        return False, ("groupInfo.deadline=%d != наш deadline=%d (чужой слот/протухший переарм)"
                       % (dl, expected_deadline))
    if int(n) != len(borrowers):
        return False, "groupInfo.n=%d != число отправленных целей=%d" % (n, len(borrowers))
    for i, want in enumerate(borrowers):
        got, _, _, _ = read_target_info(call_fn, addr, g, i)
        if not _addr_eq(got, want):
            return False, ("targetInfo(%d,%d).borrower=%s != наш %s (#84: сверки одного borrower "
                           "мало, кит держится в топе)" % (g, i, got, want))
    return True, "арм сел по цепи: deadline+n+%d borrower'ов совпали" % len(borrowers)


def verify_hot(call_fn, addr, g0, count, until, our_receipt=None, head_fn=None):
    """§8.2: указатель сел и наведён верно — hotInfo() == наши g0/count/until (с цепи).

    Читаем не раньше блока нашей записи (см. `_await_head`): 16.08 севший setHot прочитался
    как (0,0,0) с отставшего узла балансировщика."""
    land = (our_receipt or {}).get("blockNumber") if our_receipt else None
    if not _await_head(head_fn, land):
        return False, ("узел не дорос до блока записи %s — состояние указателя НЕИЗВЕСТНО" % land)
    rg0, rcnt, runtil = read_hot_info(call_fn, addr)
    if (rg0, rcnt, runtil) != (int(g0), int(count), int(until)):
        return False, ("hotInfo()=(%d,%d,%d) != наш (%d,%d,%d)"
                       % (rg0, rcnt, runtil, g0, count, until))
    return True, "указатель сел: hotInfo == (%d,%d,%d)" % (g0, count, until)


def arm_gas_limit(n_targets, route_bytes) -> int:
    """§3: лимит газа арма от ЗАМЕРЕННОГО (12 757 108 на 8 целей/2КБ) с запасом, НЕ от лимита v5.

    Формула масштабируется входом (число целей × размер роута — байт роута ложится в холодный
    SSTORE ~625/байт) и снизу подпёрта эталоном×запас: недооценка = мёртвый арм (#84), а щедрый
    лимит по EIP-1559 ничего не стоит (платим за gasUsed). Для (8, 2048) отдаёт >= ARM_GAS_REF×запас.
    """
    n, rb = int(n_targets), int(route_bytes)
    per_target = 300_000 + 900 * rb        # фикс.слоты цели + байты роута в холодный сторадж
    formula = 500_000 + n * per_target
    ref_scaled = int(ARM_GAS_REF * ARM_GAS_SAFETY * max(1, n) / 8)
    return max(formula, ref_scaled, _ARM_GAS_FLOOR if (n >= 8 and rb >= 2048) else 0)


# ==================================================================================================
# §9. minOut: КОНСЕРВАТИВНЫЙ вариант — группа собирается только из целей с НЕПЕРЕСЕКАЮЩИМИСЯ пулами.
#     Пересечение = ГРОМКИЙ отказ при сборке (не тихое наследование v5-minOut). См. отчёт: это
#     сужает каскад одного пула — назван как ограничение пакета.
# ==================================================================================================
class PoolOverlap(Exception):
    """Две цели группы делят пул: minOut второй посчитан ДО свопа первой (§9). Консервативно —
    отказ при сборке, а не молчаливый недоучёт проскальзывания предыдущих свопов."""


def assert_disjoint_pools(pools):
    """pools: список множеств пул-идентификаторов (по одному на цель группы). Пусто/None у цели
    трактуем как «пул неизвестен» => пересекается со всем (незнание закрывает гард)."""
    seen = set()
    for idx, ps in enumerate(pools):
        if not ps:
            raise PoolOverlap("§9: пул цели #%d НЕИЗВЕСТЕН — консервативно считаем пересечением "
                              "(незнание закрывает гард); дай явный пул или разнеси в группы" % idx)
        ps = set(ps)
        inter = ps & seen
        if inter:
            raise PoolOverlap("§9: цель #%d делит пул %s с предыдущей целью группы — minOut второй "
                              "посчитан ДО свопа первой; консервативно НЕ группируем" % (idx, inter))
        seen |= ps


# ==================================================================================================
# §2. РАСКЛАДКА (чистые функции, тестируются без форка). Инвариант: спан = ОДИН mp.
# ==================================================================================================
DEFAULT_COUNT = 1                 # §2: концентрация > покрытие, пока звено Г (прибытие) не решено
EV_MARGIN = float(os.environ.get("SPEC_FIRE_V2_EV_MARGIN", "0.2"))   # гистерезис #88 на уровне группы


def order_targets_by_ev(items):
    """items: список (ev, target). Возврат: targets, упорядоченные по EV УБЫВАНИЕМ (стабильно).
    EV считает стратегия (debt×близость-к-флипу, _plan_ev_val) — сюда приходит готовым, раскладка
    от формулы EV развязана."""
    return [t for _, t in sorted(enumerate(items), key=lambda kv: (-float(kv[1][0]), kv[0]))]


def plan_market_groups(mp, items, *, max_per_group=8, pools=None):
    """Каскад ОДНОГО рынка (mp) -> спан ПОДРЯД идущих групп по <=8 целей (§2). count = число групп.

    items: [(ev, target)]. pools (опц.): [set] по цели — если задано, каждая ГРУППА собирается
    только из целей с непересекающимися пулами (§9, консервативно; иначе PoolOverlap).
    Возврат: {"mp": mp, "count": k, "groups": [[target,...], ...]} — все группы несут ОДИН mp.
    """
    if not items:
        raise ValueError("пустой каскад — группировать нечего")
    order = list(sorted(range(len(items)), key=lambda i: (-float(items[i][0]), i)))
    ordered_targets = [items[i][1] for i in order]
    ordered_pools = [set(pools[i]) if pools else None for i in order] if pools is not None else None
    groups, gp = [], []
    grp_pools = []
    for k, t in enumerate(ordered_targets):
        gp.append(t)
        if ordered_pools is not None:
            grp_pools.append(ordered_pools[k])
        if len(gp) == max_per_group:
            if ordered_pools is not None:
                assert_disjoint_pools(grp_pools)
            groups.append(gp); gp = []; grp_pools = []
    if gp:
        if ordered_pools is not None:
            assert_disjoint_pools(grp_pools)
        groups.append(gp)
    return {"mp": mp_tuple(mp), "count": len(groups), "groups": groups}


def choose_eviction(slots, incoming_ev, *, ev_margin=EV_MARGIN):
    """Арбитраж при нехватке групп (§2): 8 занято, пришёл рынок с EV incoming_ev.

    slots: [{"g": idx, "ev": float, "live_span": bool}]. Живой спан НЕ вытесняется вовсе (§1/§2 —
    вытеснение теперь ревертит HotSpanLive). Вытесняется группа с НАИМЕНЬШИМ EV и только если
    новый превосходит её на ПОРОГ (incoming > lowest·(1+margin)) — перенос гистерезиса #88 со
    слота на группу. Возврат: индекс группы к вытеснению, либо None (не превосходит порог / все
    под живым спаном).
    """
    free = [s for s in slots if not s.get("live_span")]
    if len(slots) < 8:
        return None                      # есть свободный слот — вытеснять некого
    if not free:
        return None                      # все 8 под живым спаном — новый ждёт (§1: реверт вместо увода)
    victim = min(free, key=lambda s: float(s["ev"]))
    if float(incoming_ev) > float(victim["ev"]) * (1.0 + ev_margin):
        return victim["g"]
    return None                          # не лучше слабейшего на порог — держим (демпфер флапа #88)


# ==================================================================================================
# ОРКЕСТРАЦИЯ (I/O инжектируется). Транспорт — образец send_board_tx v5; проводит сессия.
# ==================================================================================================
def arm_group(send_fn, call_fn, addr, g, mp, deadline, gate_trigger, targets, *,
              route_bytes=None, gas=None, head_fn=None):
    """Заармить группу и ПОДТВЕРДИТЬ по цепи (§3). send_fn(calldata, gas)->receipt (квитанция
    НАШЕГО хэша), call_fn(to,data)->bytes. Возврат (ok, receipt, reason). НЕ признаёт арм по хэшу.
    """
    cd = encode_set_group(g, mp, deadline, gate_trigger, targets)
    if gas is None:
        rb = route_bytes if route_bytes is not None else max((len(t[4]) for t in targets), default=0)
        gas = arm_gas_limit(len(targets), rb)
    rec = send_fn(cd, gas)
    borrowers = [t[0] for t in targets]
    ok, reason = verify_arm(call_fn, addr, g, rec, deadline, borrowers, head_fn=head_fn)
    return ok, rec, reason


def set_hot_pointed(send_fn, call_fn, addr, g0, count, mp, *, gas=None, head_fn=None):
    """§4: навести указатель на спан [g0,g0+count). until берётся С ЦЕПИ (min дедлайнов спана),
    mpHash — от армленных параметров. Подтверждение по цепи (verify_hot). Возврат (ok, receipt,
    reason, until)."""
    until = until_from_chain(call_fn, addr, g0, count)
    if not mphash_from_chain_matches(call_fn, addr, g0, count, mp):
        return False, None, "§2.5б: не все группы спана несут рынок mp (setHot откатит по отпечатку)", until
    cd = encode_set_hot(g0, count, until, mp_hash(mp))
    rec = send_fn(cd, gas if gas is not None else 200_000)
    if status_of(rec) != 1:
        return False, rec, "setHot квитанция status!=1 (until>deadline спана? отпечаток?)", until
    ok, reason = verify_hot(call_fn, addr, g0, count, until, our_receipt=rec, head_fn=head_fn)
    return ok, rec, reason, until
