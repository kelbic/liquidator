"""ПРИЁМКА ПРОВОДКИ v6-D4 ЗА ФЛАГОМ (задача #135). Без сети.

Предмет: `SPEC_FIRE_V6` — переключатель боевого тракта с однослотовой доски v5 на мультислот
v6-D4. Главное требование ТЗ стоит ПЕРВЫМ пунктом и оно же самое дорогое: при флаге=0 тракт v5
обязан работать БИТ-В-БИТ. Остальное — приёмка включённой полярности.

Каноны, по которым построен стенд:
 * «ШОВ ПОДМЕНЁН ВЫШЕ ДЕФЕКТА»: проверка строкой в исходнике проводкой НЕ является. Здесь
   исполняется НАСТОЯЩИЙ `_arm_board`, НАСТОЯЩИЙ `presign_ladder` (с реальной подписью
   eth_account), НАСТОЯЩИЙ `send_owner_tx_v6` и НАСТОЯЩИЙ `ChainIndex._ingest`. Подменён
   только транспорт (узел), то есть шов НИЖЕ проверяемой логики.
 * «ПРИЁМКА — ЭТАЛОН, А НЕ САМООТЧЁТ»: байт-в-байт совпадение калдаты v5 доказывается
   ОТДЕЛЬНЫМ ПРОХОДОМ — тот же сценарий исполняется в ПОДПРОЦЕССЕ на дереве БАЗОВОГО коммита
   (кода до этой правки не существует в нашем процессе вовсе).
 * ПАРА КРАСНЫЙ/ЗЕЛЁНЫЙ у каждого пункта: измерение обязано уметь провалиться.
 * «НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД, НО НЕ ЗАПИРАЕТ ОГОНЬ»: отставший узел даёт «состояние
   НЕИЗВЕСТНО» и деградацию в V1, а НЕ ложное «арм не сел».

ЗАПУСК: python3 test_v6_wiring.py      (ненулевой код = провал приёмки)
Служебный режим (для подпроцесса на базовом дереве): --emit-v5-calldata
"""
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eth_abi import decode as abi_decode          # noqa: E402
from eth_abi import encode as abi_encode          # noqa: E402
from eth_account import Account                   # noqa: E402

# ===================================================================================================
# ОБЩИЙ СЦЕНАРИЙ АРМА. Живёт до импорта проверяемых модулей, чтобы подпроцесс на БАЗОВОМ дереве
# исполнял РОВНО ТОТ ЖЕ вход. Ни одного числа из окружения: калдата обязана быть детерминирована.
# ===================================================================================================
FROZEN_NOW = 1_700_000_000.0                      # часы заморожены: dl входит в калдату setPlan


def _a(tag):
    return "0x" + (tag * 40)[:40]


FAT = _a("f")
PLAN = {"mp": {"loanToken": _a("1"), "collateralToken": _a("2"), "oracle": _a("3"),
               "irm": _a("4"), "lltv": 860000000000000000},
        "borrower": FAT, "repaid_shares": 12345, "router": _a("5"),
        "swap_calldata": "0x" + "ab" * 800, "min_profit": 7, "trigger": 2 * 10 ** 36}
PROBE = {"calldata": "0x" + "cd" * 900, "gas": 625_000, "swap_meta": None, "plan": PLAN}
MID = "0xbtc"


class _FrozenTime:
    """Проксирует настоящий time, кроме time() — иначе дедлайн плана (а с ним и калдата)
    зависел бы от секунды запуска, и сравнение двух проходов было бы бессмысленным."""

    def __getattr__(self, k):
        return getattr(time, k)

    @staticmethod
    def time():
        return FROZEN_NOW


def build_sfl(armed_sink, log_sink=None):
    """НАСТОЯЩИЙ SpecFireLive с заглушённым транспортом (шов НИЖЕ проверяемой логики)."""
    import chain.spec_fire_live as sfl_mod
    from chain.spec_fire_live import SpecFireLive
    sfl_mod.SPEC_FIRE_PEAK_OWED = False
    sfl_mod.SPEC_FIRE_V2_BOARD = True             # без флага доски арм запрещён по построению
    sfl_mod.time = _FrozenTime()
    cfg = types.SimpleNamespace(liquidator_address=_a("9"), gas_limit_est=500_000, tip_gwei=0.05,
                                sequencer_rpc="off", eth_price_usd=1730.0,
                                fast_lane_max_repaid_usd=500_000.0, wallet_key=None)
    shared = types.SimpleNamespace(lock=threading.Lock(), groups={MID: [FAT]},
                                   debt_by={(MID, FAT): 1_500_000.0}, by_id={},
                                   hf_by={})
    sf = SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                      dev_by={"BTC": 0.0010}, build_fn=lambda m, b, d: PROBE,
                      log_fn=(log_sink.append if log_sink is not None else (lambda m: None)),
                      now=lambda: 100000.0, sleep=lambda s: None)
    sf._board_arm_fn = lambda cd, gas, tag: (armed_sink.append((cd, gas, tag)), "0xarm")[1]
    sf._verify_board = lambda *a, **k: None       # тред приёмки v5 сети не получит
    sf._arm_retry = lambda *a, **k: None
    return sf


def emit_v5_calldata():
    """Один прогон НАСТОЯЩЕГО `_arm_board` при флаге=0 -> калдата и лимит газа управляющей tx.
    Печатается JSON'ом: этот же код исполняется в подпроцессе на БАЗОВОМ дереве."""
    armed = []
    sf = build_sfl(armed)
    ok = sf._arm_board(PROBE, MID, FAT, "BTC", debt=1_500_000.0)
    return {"ok": bool(ok), "armed": [[cd, gas, tag] for cd, gas, tag in armed]}


if "--emit-v5-calldata" in sys.argv:
    print(json.dumps(emit_v5_calldata()))
    sys.exit(0)

# ===================================================================================================
import chain.board_v6 as bd                       # noqa: E402
import chain.execute as ex                        # noqa: E402
import chain.spec_fire_live as sfl_mod            # noqa: E402

RESULTS, FAILS = [], []


def verdict(point, what, ok, extra=""):
    RESULTS.append((point, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
    extra = "" if extra is None else str(extra)
    print("  %s %-10s %s%s" % ("[OK]  " if ok else "[FAIL]", point, what,
                               (" | " + extra) if extra else ""))
    if not ok:
        FAILS.append(point)


def set_flag(on: bool):
    """Флаг ставится АТРИБУТОМ модуля — так его читают и `probe_calldata`, и `_arm_board`."""
    bd.SPEC_FIRE_V6 = bool(on)


set_flag(False)

print("=" * 100)
print("ПРИЁМКА #135: проводка v6-D4 за флагом SPEC_FIRE_V6")
print("=" * 100)

# ===================================================================================================
# П0. ДЕФОЛТ ФЛАГА. Выключатель боевого тракта обязан быть выключен из коробки И не включаться
#     пустой переменной (опечатка в .env не имеет права перевести флот на другой контракт).
# ===================================================================================================
print("\nП0. Дефолт и форма флага")
_env_cases = {}
for _v, _want in (("", False), ("0", False), ("no", False), ("1", True), ("on", True)):
    _old = os.environ.get("SPEC_FIRE_V6")
    os.environ["SPEC_FIRE_V6"] = _v
    _env_cases[_v] = bd._env_on("SPEC_FIRE_V6")
    if _old is None:
        os.environ.pop("SPEC_FIRE_V6", None)
    else:
        os.environ["SPEC_FIRE_V6"] = _old
os.environ.pop("SPEC_FIRE_V6", None)
verdict("П0", "SPEC_FIRE_V6: пусто/0/no -> ВЫКЛ, 1/on -> ВКЛ (белый список, не чёрный)",
        _env_cases == {"": False, "0": False, "no": False, "1": True, "on": True},
        str(_env_cases))

# ===================================================================================================
# П1 (приёмка «а»). ФЛАГ=0: КАЛДАТА setPlan БАЙТ-В-БАЙТ КАК В БАЗЕ.
#     Эталон — ОТДЕЛЬНЫЙ ПРОХОД на дереве базового коммита (подпроцесс), а не наше чтение.
# ===================================================================================================
print("\nП1 (а). Флаг=0: encode_set_plan реально зовётся, калдата == база (эталон из подпроцесса)")
set_flag(False)
cur = emit_v5_calldata()
cur_ok = bool(cur["ok"]) and len(cur["armed"]) == 1 and cur["armed"][0][2] == "setPlan"
_setplan_sel = "0x" + __import__("eth_utils").keccak(text=ex._SET_PLAN_SIG)[:4].hex()
verdict("П1.1", "флаг=0: ушла РОВНО одна управляющая tx с тегом setPlan и селектором %s"
        % _setplan_sel[:10],
        cur_ok and cur["armed"][0][0].startswith(_setplan_sel),
        "tag=%s len=%d" % (cur["armed"][0][2] if cur["armed"] else "-",
                           len(cur["armed"][0][0]) if cur["armed"] else 0))


def _base_ref():
    """Коммит ДО этой правки: первый по истории, чей spec_fire_live НЕ знает `_arm_board_v6`.
    Ищется по дереву, а не задаётся числом — иначе эталон протухает от каждого коммита."""
    if os.environ.get("V6_BASE_REF"):
        return os.environ["V6_BASE_REF"]
    shas = subprocess.run(["git", "rev-list", "-n", "40", "HEAD"], capture_output=True,
                          text=True).stdout.split()
    for sha in shas:
        src = subprocess.run(["git", "show", "%s:chain/spec_fire_live.py" % sha],
                             capture_output=True, text=True).stdout
        if src and "_arm_board_v6" not in src:
            return sha
    raise SystemExit("БАЗОВЫЙ КОММИТ НЕ НАЙДЕН: сравнивать не с чем")


BASE = _base_ref()
import tempfile                                    # noqa: E402
import shutil                                      # noqa: E402

_tmp = tempfile.mkdtemp(prefix="v6base-")
try:
    with open(os.path.join(_tmp, "base.tar"), "wb") as fh:
        subprocess.run(["git", "archive", BASE], stdout=fh, check=True)
    subprocess.run(["tar", "-xf", os.path.join(_tmp, "base.tar"), "-C", _tmp], check=True)
    shutil.copy(__file__, os.path.join(_tmp, "test_v6_wiring.py"))
    _base_src = pathlib.Path(_tmp, "chain", "spec_fire_live.py").read_text()
    _env = dict(os.environ)
    _env.pop("SPEC_FIRE_V6", None)
    _p = subprocess.run([sys.executable, "test_v6_wiring.py", "--emit-v5-calldata"],
                        cwd=_tmp, capture_output=True, text=True, env=_env)
    base = json.loads(_p.stdout.strip().splitlines()[-1]) if _p.returncode == 0 else None
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

verdict("П1.2", "подпрогон на БАЗЕ %s исполнил код БЕЗ проводки v6 (позитивный контроль эталона)"
        % BASE[:9],
        base is not None and "SPEC_FIRE_V6" not in _base_src and "_arm_board_v6" not in _base_src,
        (_p.stderr or "")[-160:] if base is None else "")
verdict("П1.3", "ФЛАГ=0 -> калдата и лимит газа setPlan БАЙТ-В-БАЙТ равны базовым",
        base is not None and base["armed"] == cur["armed"] and base["ok"] == cur["ok"],
        "база=%s тек=%s" % ((base or {}).get("armed", [["-", "-", "-"]])[0][0][:26],
                            cur["armed"][0][0][:26] if cur["armed"] else "-"))

# КРАСНЫЙ КОНТРОЛЬ ТОГО ЖЕ ИЗМЕРЕНИЯ: при флаге=1 калдата ОБЯЗАНА разойтись с базовой.
# Без него «совпало» неотличимо от «сравниваем строку саму с собой».
set_flag(True)
armed_v6_probe = []
_sf_red = build_sfl(armed_v6_probe)
_sf_red._v6_call_fn = lambda to, data: (_ for _ in ()).throw(RuntimeError("узла нет"))
_red_ok = _sf_red._arm_board(PROBE, MID, FAT, "BTC", debt=1_500_000.0)
verdict("П1.4", "КРАСНЫЙ КОНТРОЛЬ: флаг=1 -> setPlan НЕ отправляется вовсе (ветки разные)",
        _red_ok is False and armed_v6_probe == [],
        "armed=%r" % armed_v6_probe)
set_flag(False)

# ===================================================================================================
# П2 (приёмка «в»). БАЙТ ПРОБЫ НА РЕАЛЬНО СОБРАННОЙ ЛЕСТНИЦЕ.
#     Байт запечён В ПОДПИСЬ, поэтому читаем его из ПОДПИСАННЫХ БАЙТОВ, а не из константы.
# ===================================================================================================
print("\nП2 (в). Байт пробы на РЕАЛЬНО подписанной лестнице (декодируем raw-подпись)")
import rlp                                          # noqa: E402

LEG = Account.create()


class _FakeEth:
    """Узел. Подпись — НАСТОЯЩАЯ (eth_account), сеть — фальшивая."""
    account = Account

    def __init__(self):
        self.pending = 5
        self.sent = []
        self.fail_nonces = set()
        self.receipts = {}

    def get_transaction_count(self, addr, blk="pending"):
        return self.pending

    max_priority_fee = 1_000_000     # v5-путь (send_tx) сам добирает fee, если их не дали

    def get_block(self, _x):
        return {"baseFeePerGas": 5_000_000}

    def get_balance(self, _a):
        return 10 ** 18

    def send_raw_transaction(self, raw):
        raw = bytes(raw)
        tx = rlp.decode(raw[1:]) if raw[:1] == b"\x02" else None
        nonce = int.from_bytes(tx[1], "big") if tx and tx[1] else 0
        self.sent.append({"nonce": nonce,
                          "maxPriorityFeePerGas": int.from_bytes(tx[2], "big"),
                          "maxFeePerGas": int.from_bytes(tx[3], "big"),
                          "data": "0x" + tx[7].hex()})
        if nonce in self.fail_nonces:
            raise ValueError({"code": -32000,
                              "message": "replacement transaction underpriced"})
        h = bytes([nonce]) + b"\x00" * 31
        self.receipts[h.hex()] = {"status": 1, "blockNumber": 1000, "transactionHash": h.hex()}
        return h

    def get_transaction_receipt(self, h):
        return self.receipts.get(h if isinstance(h, str) else h.hex())


class _FakeW3:
    def __init__(self):
        self.eth = _FakeEth()


class _FakeRpc:
    chain_id = 8453
    rpc_url = "http://stand.invalid"

    def __init__(self):
        self._w3 = _FakeW3()

    def _web3(self):
        return self._w3


def build_ladder(flag_on):
    set_flag(flag_on)
    ex._VOLLEY_LADDER.pop(LEG.address, None)
    ex._VOLLEY_NONCE.pop(LEG.address, None)
    ex._VOLLEY_LOCKS[LEG.address] = threading.Lock()
    ex._VOLLEY_BAL[LEG.address] = [10 ** 18, time.monotonic()]
    rpc = _FakeRpc()
    cfg = types.SimpleNamespace(liquidator_address=_a("9"))
    ex.presign_ladder(rpc, cfg, LEG.address, LEG.key.hex(), 900_000, depth=2,
                      base_fee=5_000_000, tip=1_000_000)
    st = ex._VOLLEY_LADDER[LEG.address]
    raw = bytes.fromhex(st["q"][0][1][2:])
    fields = rlp.decode(raw[1:])                    # 0x02 || rlp([chainId,nonce,...,data,...])
    return "0x" + fields[7].hex(), st


_b0, _st0 = build_ladder(False)
_b1, _st1 = build_ladder(True)
verdict("П2.1", "флаг=0 -> в ПОДПИСАННОЙ пробе байт 0x01 (v5); флаг=1 -> 0xff (сентинел _hot)",
        _b0 == "0x01" and _b1 == "0xff", "0:%s 1:%s" % (_b0, _b1))
# Смена флага обязана делать УЖЕ ПОДПИСАННУЮ лестницу протухшей: иначе 110 ног стреляют
# байтом от чужого контракта, а счётчик проб при этом растёт (мёртвый прибор с живым числом).
set_flag(False)
_stale_after_flip = ex.ladder_resign_needed(LEG.address, 5_000_000, 1_000_000)
set_flag(True)
_fresh_same_flag = ex.ladder_resign_needed(LEG.address, 5_000_000, 1_000_000)
verdict("П2.2", "смена флага делает подписанную лестницу ПРОТУХШЕЙ (пересборка), при том же "
        "флаге — нет", _stale_after_flip is True and _fresh_same_flag is False,
        "после смены=%s при своём=%s" % (_stale_after_flip, _fresh_same_flag))
set_flag(False)

# ===================================================================================================
# ФАЛЬШИВЫЙ КОНТРАКТ v6: хранит РОВНО то, что декодировал из НАШЕЙ калдаты, и отвечает на
# groupInfo/targetInfo/hotInfo. Кодирование и декодирование — настоящие (eth_abi), поэтому
# ошибка в сигнатурах board_v6 стенд обрушит, а не спрячет.
# ===================================================================================================
class FakeV6:
    def __init__(self):
        self.groups, self.targets = {}, {}
        self.hot = (0, 0, 0)
        self.block = 1000
        self.calls = []
        self.sent = []
        self.arm_status = 1
        self.is_v6 = True

    def call(self, to, data):
        sel = data[2:10]
        self.calls.append(sel)
        if not self.is_v6:
            raise RuntimeError("execution reverted (это v5, hotInfo нет)")
        if sel == bd.selector("hotInfo").hex():
            return abi_encode(["uint8", "uint8", "uint64"], list(self.hot))
        arg = bytes.fromhex(data[10:])
        if sel == bd.selector("groupInfo").hex():
            g = abi_decode(["uint256"], arg)[0]
            gr = self.groups.get(g)
            if gr is None:
                return abi_encode(["address", "uint64", "uint256", "uint8"],
                                  [bd.ZERO_ADDR, 0, 0, 0])
            return abi_encode(["address", "uint64", "uint256", "uint8"],
                              [gr["mp"][2], gr["dl"], gr["gate"], len(gr["targets"])])
        if sel == bd.selector("targetInfo").hex():
            g, i = abi_decode(["uint256", "uint256"], arg)
            t = self.groups.get(g, {}).get("targets", [])[i]
            return abi_encode(["address", "uint128", "uint128", "uint256"],
                              [t[0], t[1], t[2], t[7]])
        raise RuntimeError("неизвестный селектор %s" % sel)

    def send(self, cd, gas):
        sel = cd[2:10]
        arg = bytes.fromhex(cd[10:])
        self.sent.append((sel, gas))
        if sel == bd.selector("setGroup").hex():
            g, mp, dl, gate, ts = abi_decode(
                ["uint256", bd._MP_T, "uint64", "uint256", "%s[]" % bd._TARGET_T], arg)
            if self.arm_status == 1:
                self.groups[g] = {"mp": mp, "dl": dl, "gate": gate, "targets": [list(t) for t in ts]}
            self.block += 1
            return {"status": self.arm_status, "blockNumber": self.block,
                    "transactionHash": "0x" + "aa" * 32}
        if sel == bd.selector("setHot").hex():
            g0, cnt, until, _mph = abi_decode(["uint8", "uint8", "uint64", "bytes32"], arg)
            self.hot = (g0, cnt, until)
            self.block += 1
            return {"status": 1, "blockNumber": self.block, "transactionHash": "0x" + "bb" * 32}
        if sel == bd.selector("clearHot").hex():
            self.hot = (0, 0, 0)
            self.block += 1
            return {"status": 1, "blockNumber": self.block, "transactionHash": "0x" + "cc" * 32}
        if sel == bd.selector("clearTarget").hex():
            g, i = abi_decode(["uint256", "uint256"], arg)
            gr = self.groups.get(g)
            if gr:
                gr["targets"][i][0] = bd.ZERO_ADDR        # контракт делает delete ts[i]
            self.block += 1
            return {"status": 1, "blockNumber": self.block, "transactionHash": "0x" + "dd" * 32}
        raise RuntimeError("неизвестный селектор %s" % sel)


def arm_v6(head_lag=0, arm_status=1, is_v6=True, logs=None):
    """НАСТОЯЩИЙ `_arm_board` при флаге=1 против фальшивого узла."""
    set_flag(True)
    node = FakeV6()
    node.arm_status = arm_status
    node.is_v6 = is_v6
    armed = []
    sf = build_sfl(armed, log_sink=logs)
    sf._v6_call_fn = node.call
    sf._v6_send_fn = node.send
    sf._v6_head_fn = lambda: node.block - head_lag
    ok = sf._arm_board(PROBE, MID, FAT, "BTC", debt=1_500_000.0)
    return ok, sf, node, armed


# board_v6._await_head спит по секунде между попытками — стенду сон не нужен, а логика гарда
# проверяется та же (подменён СОН, не гард).
bd.time = types.SimpleNamespace(sleep=lambda s: None, time=time.time)

# ===================================================================================================
# П3. ФЛАГ=1: ИДЁТ setGroup + setHot, АРМ ПОДТВЕРЖДЁН ЧТЕНИЕМ (не хэшем).
# ===================================================================================================
print("\nП3. Флаг=1: setGroup + setHot, приёмка ПО ЦЕПИ (сверка эталоном — сторадж узла)")
ok3, sf3, node3, armed3 = arm_v6()
_sels = [s for s, _g in node3.sent]
_grp = node3.groups.get(bd.SPEC_FIRE_V6_GROUP, {})
verdict("П3.1", "ушли РОВНО setGroup+setHot, v5-калдата не отправлялась",
        ok3 is True and _sels == [bd.selector("setGroup").hex(), bd.selector("setHot").hex()]
        and armed3 == [], "sels=%s v5=%r" % (_sels, armed3))
verdict("П3.2", "ЭТАЛОН — СТОРАДЖ: в группе лежит наш borrower, дедлайн и n; указатель "
        "наведён на неё с until С ЦЕПИ",
        bool(_grp) and _grp["targets"][0][0].lower() == FAT.lower()
        and node3.hot == (bd.SPEC_FIRE_V6_GROUP, 1, _grp["dl"]),
        "hot=%s dl=%s" % (node3.hot, _grp.get("dl")))
verdict("П3.3", "§6: заявка по repaidShares (seizedAssets=0) — инвариантна к падению оракула",
        _grp["targets"][0][1] == PLAN["repaid_shares"] and _grp["targets"][0][2] == 0)
_route_b = len(PLAN["swap_calldata"]) // 2 - 1
_v5_gas = max(700_000, 300_000 + 800 * (len(cur["armed"][0][0]) // 2))   # формула v5 на той же цели
verdict("П3.4", "лимит газа арма = замеренный эталон на цель (%d), и он ВЫШЕ формулы v5 (%d)"
        % (bd.arm_gas_limit(1, _route_b), _v5_gas),
        node3.sent[0][1] == bd.arm_gas_limit(1, _route_b) > _v5_gas,
        "gas=%d" % node3.sent[0][1])
verdict("П3.5", "доска признана боевой (verified) и счётчик v6-армов вырос",
        sf3._board.get("verified") is True and sf3.stats["board_v6_arms"] == 1
        and sf3.stats["board_v6_unlanded"] == 0 and sf3.stats["board_v6_unknown"] == 0,
        str({k: v for k, v in sf3.stats.items() if k.startswith("board_v6")}))

# КРАСНЫЙ: арм не сел (status=0) — доска обязана быть СНЯТА, а не «armed по хэшу» (#84).
ok3r, sf3r, node3r, _ = arm_v6(arm_status=0)
verdict("П3.6", "КРАСНЫЙ: квитанция status=0 -> «не сел» (не armed по хэшу), огонь в V1",
        ok3r is False and sf3r._board == {} and sf3r.stats["board_v6_unlanded"] == 1
        and sf3r.stats["board_v6_unknown"] == 0, str(sf3r.stats["board_v6_unlanded"]))

# ===================================================================================================
# П4 (приёмка «г»). ГАРД ОТСТАВАНИЯ УЗЛА ПРОКИНУТ.
#     Отставший узел обязан дать «состояние НЕИЗВЕСТНО», а не «арм не сел».
# ===================================================================================================
print("\nП4 (г). Гард отставания узла: отставший узел = НЕЗНАНИЕ, а не приговор")
_logs4 = []
ok4, sf4, node4, _ = arm_v6(head_lag=5, logs=_logs4)
_said_unknown = any("НЕИЗВЕСТНО" in m for m in _logs4)
verdict("П4.1", "head_fn отстаёт -> счётчик board_v6_unknown, НЕ board_v6_unlanded",
        ok4 is False and sf4.stats["board_v6_unknown"] == 1
        and sf4.stats["board_v6_unlanded"] == 0,
        str({k: v for k, v in sf4.stats.items() if k.startswith("board_v6")}))
verdict("П4.2", "в журнале ГРОМКО сказано «состояние НЕИЗВЕСТНО» (иначе чинили бы газ вместо RPC)",
        _said_unknown, [m for m in _logs4 if "v6" in m][:1])
verdict("П4.3", "ПОЗИТИВНЫЙ КОНТРОЛЬ того же гарда: узел не отстаёт -> тот же сценарий ЗЕЛЁН",
        ok3 is True)

# КРАСНЫЙ КОНТРОЛЬ ПРОКИДКИ: без head_fn (как было бы при забытом параметре) тот же
# отставший узел даёт ЛОЖНОЕ «арм не сел» — то есть гард действительно приносит пользу.
_bd_arm = bd.arm_group
_no_head = {}


def _arm_no_head(send_fn, call_fn, addr, g, mp, dl, gate, targets, **kw):
    kw.pop("head_fn", None)
    return _bd_arm(send_fn, call_fn, addr, g, mp, dl, gate, targets, **kw)


class _LaggingCall:
    """Узел, который ОТСТАЛ: групп ещё не видит, хотя квитанция уже есть."""

    def __init__(self, node):
        self.node = node

    def __call__(self, to, data):
        if data[2:10] == bd.selector("groupInfo").hex():
            return abi_encode(["address", "uint64", "uint256", "uint8"], [bd.ZERO_ADDR, 0, 0, 0])
        return self.node.call(to, data)


set_flag(True)
node4r = FakeV6()
sf4r = build_sfl([])
sf4r._v6_call_fn = _LaggingCall(node4r)
sf4r._v6_send_fn = node4r.send
sf4r._v6_head_fn = lambda: node4r.block - 5
bd.arm_group = _arm_no_head
try:
    ok4r = sf4r._arm_board(PROBE, MID, FAT, "BTC", debt=1_500_000.0)
finally:
    bd.arm_group = _bd_arm
verdict("П4.4", "КРАСНЫЙ КОНТРОЛЬ ПРОКИДКИ: если head_fn НЕ прокинут — тот же отставший узел "
        "даёт ложное «не сел» (unlanded), а не «неизвестно»",
        ok4r is False and sf4r.stats["board_v6_unlanded"] == 1
        and sf4r.stats["board_v6_unknown"] == 0,
        str({k: v for k, v in sf4r.stats.items() if k.startswith("board_v6")}))

# ===================================================================================================
# П5. ГАРД АДРЕСА: флаг=1 на контракте v5 обязан отказать ГРОМКО и не тратить tx.
# ===================================================================================================
print("\nП5. Гард адреса: SPEC_FIRE_V6=1 при LIQUIDATOR_ADDRESS от v5")
_logs5 = []
ok5, sf5, node5, _ = arm_v6(is_v6=False, logs=_logs5)
verdict("П5", "адрес не отвечает на hotInfo() -> отказ, НИ ОДНОЙ tx, причина названа поимённо",
        ok5 is False and node5.sent == [] and sf5.stats["board_v6_wrong_addr"] == 1
        and any("НЕ v6" in m for m in _logs5), [m for m in _logs5][:1])

# ===================================================================================================
# П6 (приёмка «д»). PENDING-NONCE: занятый номер лечится СЛЕДУЮЩИМ, а НЕ ценой.
#     Исполняется НАСТОЯЩИЙ send_owner_tx_v6; подменён только узел.
# ===================================================================================================
print("\nП6 (д). Коллизия nonce: следующий свободный, цена НЕ поднимается")
_key = Account.create().key.hex()
_cfg6 = types.SimpleNamespace(liquidator_address=_a("9"), wallet_key=_key, tip_gwei=0.05)
rpc6 = _FakeRpc()
rpc6._w3.eth.pending = 41
rpc6._w3.eth.fail_nonces = {41, 42}                # два подряд заняты боевыми tx бота
ex._LOCAL_NONCE.clear()
rec6 = ex.send_owner_tx_v6(rpc6, _cfg6, "0x1234", 100_000, tag="стенд")
_att = rpc6._w3.eth.sent
_prices = {(a["maxFeePerGas"], a["maxPriorityFeePerGas"]) for a in _att}
verdict("П6.1", "коллизия -> нонсы 41,42,43 подряд; квитанция получена с 43",
        rec6 is not None and [a["nonce"] for a in _att] == [41, 42, 43]
        and int(rec6["status"]) == 1, [a["nonce"] for a in _att])
verdict("П6.2", "ЦЕНА В РЕТРАЯХ НЕ МЕНЯЛАСЬ (не перебиваем свою же боевую tx)",
        len(_prices) == 1, str(_prices))
verdict("П6.3", "локальный счётчик nonce сдвинут на использованный+1 (дыры не оставили)",
        ex._LOCAL_NONCE.get(Account.from_key(_key).address) == 44,
        str(ex._LOCAL_NONCE))

# КРАСНЫЙ КОНТРОЛЬ (парный, тот же сценарий): v5-отправитель `send_board_tx` в этой же
# ситуации tx ТЕРЯЕТ — ровно то, что ловили руками 16.08.
rpc6b = _FakeRpc()
rpc6b._w3.eth.pending = 41
rpc6b._w3.eth.fail_nonces = {41}
ex._LOCAL_NONCE.clear()
_h_v5 = ex.send_board_tx(rpc6b, _cfg6, "0x1234", 100_000, tag="стенд-красный")
verdict("П6.4", "КРАСНЫЙ: прежний send_board_tx на той же коллизии теряет tx (None) — "
        "механизм ретрая по nonce действительно НОВЫЙ",
        _h_v5 is None and len(rpc6b._w3.eth.sent) == 1, str(_h_v5))
ex._LOCAL_NONCE.clear()

# ===================================================================================================
# П7. §7 clearTarget ЧЕРЕЗ НАСТОЯЩИЙ ПОДПИСЧИК ИНДЕКСА (шов не выше дефекта: гоняем
#     ChainIndex._ingest, а не наш колбэк напрямую — именно этим классом был мёртв #123).
# ===================================================================================================
print("\nП7. §7: чужая победа -> clearTarget, проводка через НАСТОЯЩИЙ ChainIndex._ingest")
from chain import chain_index as _ci                # noqa: E402

set_flag(True)
ok7, sf7, node7, _ = arm_v6()
_mid7 = "0x" + "d7" * 32
_bor7 = FAT
sf7._board_v6["slots"] = {(_mid7.lower(), _bor7.lower()): (bd.SPEC_FIRE_V6_GROUP, 0)}
_raw_log = {"address": "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb",
            "topics": [_ci.MORPHO_LIQUIDATE_TOPIC, _mid7,
                       "0x" + "00" * 12 + "ca" * 20, "0x" + "00" * 12 + _bor7[2:]],
            "data": "0x" + "00" * 320, "transactionHash": "0x" + "ee" * 32,
            "blockNumber": "0x1", "transactionIndex": "0x5", "logIndex": "0x0"}
_blk = {"number": "0x1", "timestamp": "0x1", "baseFeePerGas": "0x0",
        "transactions": [{"hash": "0x" + "ee" * 32, "transactionIndex": "0x5",
                          "maxPriorityFeePerGas": "0xf4240", "maxFeePerGas": "0xf4240",
                          "gas": "0x5208", "from": "0x" + "ca" * 20, "to": "0x" + "aa" * 20,
                          "input": "0x", "nonce": "0x0", "value": "0x0"}]}
_facts7 = _ci.build_facts(_blk, [_raw_log], watch_to=())
_idx7 = _ci.ChainIndex(url="http://stub.invalid")
_seen123 = []
_idx7.set_on_liquidate(lambda bn, evs: _seen123.append(bn))     # сосед по слоту (#123)
_idx7.add_on_liquidate(sf7.v6_on_liquidate)
_n_before = len(node7.sent)
_idx7._ingest(1, _facts7, True)
_new = [s for s, _g in node7.sent[_n_before:]]
verdict("П7.1", "чужая победа по цели группы -> ушёл clearTarget(g,i) через настоящий _ingest",
        _new == [bd.selector("clearTarget").hex()]
        and sf7.stats["board_v6_cleared_targets"] == 1, str(_new))
verdict("П7.2", "цель погашена в сторадже узла (эталон — сторадж, не наш счётчик)",
        node7.groups[bd.SPEC_FIRE_V6_GROUP]["targets"][0][0] == bd.ZERO_ADDR)
verdict("П7.3", "сосед по слоту (#123) НЕ снят армом v6 — оба подписчика отработали",
        _seen123 == [1], str(_seen123))

# СВОЯ победа: owner-tx тратить не на что (контракт гасит цель сам) — clearTarget НЕ шлём.
ok7b, sf7b, node7b, _ = arm_v6()
sf7b.cfg.liquidator_address = "0x" + "ca" * 20      # победитель = мы
sf7b._board_v6["slots"] = {(_mid7.lower(), _bor7.lower()): (bd.SPEC_FIRE_V6_GROUP, 0)}
_n_b = len(node7b.sent)
_idx7b = _ci.ChainIndex(url="http://stub.invalid")
_idx7b.add_on_liquidate(sf7b.v6_on_liquidate)
_idx7b._ingest(1, _facts7, True)
verdict("П7.4", "СВОЯ победа -> clearTarget НЕ отправляется (контракт погасил цель сам)",
        [s for s, _g in node7b.sent[_n_b:]] == []
        and sf7b.stats["board_v6_cleared_targets"] == 0)

# Флаг=0: подписчик обязан молчать даже если его позвали.
set_flag(False)
_n_c = len(node7.sent)
sf7._board_v6["slots"] = {(_mid7.lower(), _bor7.lower()): (bd.SPEC_FIRE_V6_GROUP, 0)}
sf7.v6_on_liquidate(2, [{"mid": _mid7, "borrower": _bor7, "caller": "0x" + "ca" * 20}])
verdict("П7.5", "флаг=0: подписчик v6 не шлёт НИЧЕГО даже при прямом вызове",
        len(node7.sent) == _n_c)

# ===================================================================================================
# П8. СНЯТИЕ ДОСКИ: при флаге=1 v5-калдата clearPlan (её у v6 нет вовсе) не отправляется.
# ===================================================================================================
print("\nП8. Снятие доски под флагом: clearHot вместо несуществующего у v6 clearPlan")
ok8, sf8, node8, armed8 = arm_v6()
_n8 = len(node8.sent)
sf8._clear_board(MID, FAT, "BTC", "жертву забрали")
verdict("П8", "флаг=1: снятие доски = clearHot, v5-clearPlan НЕ уходит",
        [s for s, _g in node8.sent[_n8:]] == [bd.selector("clearHot").hex()]
        and armed8 == [] and sf8._board == {},
        str([s for s, _g in node8.sent[_n8:]]))
set_flag(False)

# ===================================================================================================
print("\n" + "=" * 100)
for p, st, what in RESULTS:
    print("%-8s %-8s %s" % (p, st, what))
print("=" * 100)
if FAILS:
    print("ПРИЁМКА #135 ПРОВАЛЕНА: %s" % ", ".join(FAILS))
    sys.exit(1)
print("ПРИЁМКА #135 ПРОШЛА: %d/%d" % (len(RESULTS), len(RESULTS)))
