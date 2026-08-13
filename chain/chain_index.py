"""ФОНОВЫЙ ИНДЕКС ЦЕПИ (#110) — один потребитель блока вместо N поштучных квитанций.

ЗАЧЕМ. У нас не было честного источника правды о том, что произошло в цепи. Исход пробы
спрашивался поштучно (`eth_getTransactionReceipt`), тракт тонул в 429/JSONDecodeError (замер
12-13.08: 889 «FIRE collect batch fail» и 169 COMBAT-FAILOVER за сутки) и ЛГАЛ: пробы,
помеченные «NO-RECEIPT (nonce инвалидирован)», на самом деле были ЗАМАЙНЕНЫ (j2: сели в
49820480, +9 блоков; j1: в 49805074). Пока измерительный тракт лжёт, приёмка ЛЮБОГО пакета
читается лживым счётчиком: «probes=0», «доска не села», «нас в блоке не было» могут означать
и провал, и слепоту, а различить их нечем.

ЧТО ЭТО. Долгоживущий фоновый тред, который на каждый новый блок делает
    1) ОДИН eth_getLogs (topics-OR: AnswerUpdated оракула + Liquidate Morpho) — всегда;
    2) ОДИН eth_getBlockByNumber(fullTx=true) — если в блоке есть интересный лог ЛИБО у нас
       есть неразобранные хэши (иначе полный блок читать незачем: 1.2 МБ на блок × 43200
       блоков = 52 ГБ/сутки, чего мы себе позволить не можем);
    3) ОДИН eth_getBlockReceipts — только если в блоке нашлись НАШИ tx и узел этот метод
       поддерживает (mainnet.base.org — нет, проверено; способность выясняется один раз).
и раздаёт из памяти:
    (а) факт и статус наших tx (закрывает #109: 63k подзапросов за 2ч -> ~1 запрос на блок);
    (б) ПОЗИЦИЮ каждой нашей tx в блоке и её tip (метрика №1 плана — брать её больше неоткуда);
    (в) позицию и tip транзита оракула и позицию/tip победителя (механика приза: победитель
        стоит на транзит+1 и несёт tip транзита до wei — 6/6 разобранных блоков);
    (г) свежий список ТРУПОВ (пары рынок+заёмщик из логов Liquidate) для P1/#108.

ГРАНИЦЫ (соблюдены намеренно):
  * вне горячего пути: recv-петля только КЛАДЁТ номер блока (`offer_block`, O(1) без сети),
    вся работа — в ОДНОМ долгоживущем треде. Тред-на-событие запрещён (инцидент 15:41: Thread()
    на горячем пути пробил cgroup-кап и уронил hot_loop);
  * свой узел: CHAIN_INDEX_RPC_URL -> BG_RPC_URL_2 -> BG_RPC_URL. Боевой ключ чтений НЕ
    используется НИКОГДА: нет своего узла -> индекс не поднимается и говорит об этом ГРОМКО;
  * `bg_shed()`: бой уехал на bg-ключ -> фон уступает (условие 3 ревью 02.08). Уступка делает
    индекс протухшим, и это ВИДНО потребителю — молча пустоты он не получит;
  * учёт CU явный (батч/сырой POST мимо web3-обёртки — значит считает себя сам, иначе повтор
    дефекта «свой счётчик слеп к необслуживаемому пути»);
  * ключ в журнал не попадает: наружу только ХОСТ.

НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД. `state(h, sent_ts)` отвечает ТРЕМЯ исходами, а не двумя:
"mined" (факт с позицией), "absent" (индекс прочитал ЦЕЛИКОМ первые N блоков после отправки и
tx там нет) и "unknown" (протух / дыра / лёгкое чтение / окно ещё не закрыто / выключен).
Пустота и незнание разделены типом исхода, а не соглашением о размере словаря — ровно тот урок,
которым нам обошёлся #101. Границу "absent" называем прямо: это утверждение об ОКНЕ, а не о
цепи целиком; посадку позже окна ловит сверка nonce, и вызывающий применяет обе проверки.

ОТКАТ — МЕХАНИЗМОМ, не параметром: CHAIN_INDEX=0 не поднимает тред вовсе, `get_index()` отдаёт
None, все потребители возвращаются к прежнему пути бит-в-бит (см. docs/ROLLBACKS.md).
"""
import logging
import os
import threading
import time

log = logging.getLogger("liquidator")

# ---- топики (сверены на цепи, блок 49890266) ----
# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt) — транзит
ANSWER_UPDATED_TOPIC = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
# Liquidate(Id indexed id, address indexed caller, address indexed borrower, ...) у Morpho Blue
MORPHO_LIQUIDATE_TOPIC = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# ---- ручки ----
# ОТКАТ МЕХАНИЗМА: 0 = тред не поднимается, потребители идут прежним путём.
CHAIN_INDEX_ON = os.environ.get("CHAIN_INDEX", "1").lower() not in ("0", "false", "no", "off")


def _norm_url(u) -> str:
    return str(u or "").strip().rstrip("/")


def index_url_choice(env=None):
    """PURE: (url, источник, заметка) — КАКОЙ узел берёт индекс и почему.

    Боевого ключа чтений здесь нет намеренно: индекс — фон и не имеет права конкурировать за
    квоту с боевыми чтениями (класс #100 / RPC-страйп). Но развести его надо ЯВНО и с УЗЛОМ
    КОЛЛЕКТОРА: обе подсистемы по умолчанию берут BG_RPC_URL_2 -> BG_RPC_URL, то есть без этой
    функции они молча садились на ОДИН ключ и складывали свои темпы в одну квоту (класс «свой
    счётчик слеп к необслуживаемому пути»: каждая подсистема считает только СЕБЯ).
    Порядок: явная ручка -> первый bg-ключ, СВОБОДНЫЙ от коллектора -> общий, и тогда об этом
    говорится ГРОМКО.
    """
    e = os.environ if env is None else env
    explicit = _norm_url(e.get("CHAIN_INDEX_RPC_URL", ""))
    if explicit:
        return (explicit, "CHAIN_INDEX_RPC_URL", "явная ручка")
    col = _norm_url(e.get("SPEC_FIRE_COLLECTOR_RPC_URL", "")
                    or e.get("BG_RPC_URL_2", "") or e.get("BG_RPC_URL", ""))
    for name in ("BG_RPC_URL_2", "BG_RPC_URL"):
        u = _norm_url(e.get(name, ""))
        if u and u != col:
            return (u, name, "разведён с узлом коллектора")
    u = _norm_url(e.get("BG_RPC_URL_2", "") or e.get("BG_RPC_URL", ""))
    if u:
        return (u, "BG_RPC_URL_2/BG_RPC_URL", "ДЕЛИТ КЛЮЧ С КОЛЛЕКТОРОМ")
    return ("", "", "своего узла нет")


CHAIN_INDEX_RPC_URL, CHAIN_INDEX_URL_SRC, CHAIN_INDEX_URL_NOTE = index_url_choice()
CHAIN_INDEX_TIMEOUT = float(os.environ.get("CHAIN_INDEX_TIMEOUT", "6"))
# Сколько блоков держим в памяти (позиции/tip/наши tx). Кольцо обязано ПЕРЕЖИВАТЬ таймаут
# коллектора (SPEC_FIRE_ENGINE_COLLECT_TIMEOUT_SEC=180с ≈ 90 блоков Base по 2с): факт,
# выселенный раньше приговора, превращает «mined» в «unknown» ровно в точке применения.
# 128 блоков ≈ 4.3 мин. Памяти это стоит копейки — держим только НАШИ tx и транзиты/победы.
CHAIN_INDEX_KEEP_BLOCKS = int(os.environ.get("CHAIN_INDEX_KEEP_BLOCKS", "128"))
# Глубина списка трупов в блоках (#108): цель, убитая 30 блоков назад, симулироваться не должна.
CHAIN_INDEX_CORPSE_BLOCKS = int(os.environ.get("CHAIN_INDEX_CORPSE_BLOCKS", "30"))
# Протухание: старше этого возраста последний успешный скан = индекс НЕ источник правды.
CHAIN_INDEX_STALE_SEC = float(os.environ.get("CHAIN_INDEX_STALE_SEC", "8"))
# Догон: сколько блоков подряд дочитываем за раз. Больше — рискуем встать в петлю догона на
# каждом лаге; дальше объявляем ДЫРУ (она честно ломает право говорить «absent»).
CHAIN_INDEX_MAX_CATCHUP = int(os.environ.get("CHAIN_INDEX_MAX_CATCHUP", "4"))
# Сам спросить голову цепи, если её никто не подаёт (WS лёг / hot path выключен).
CHAIN_INDEX_SELF_POLL_SEC = float(os.environ.get("CHAIN_INDEX_SELF_POLL_SEC", "4"))
# Пытаться ли брать квитанции блока одним вызовом. Узел может не уметь (mainnet.base.org
# отвечает -32601) — тогда способность гасится НАВСЕГДА после первого отказа, без повторов.
CHAIN_INDEX_RECEIPTS = os.environ.get("CHAIN_INDEX_RECEIPTS", "1").lower() not in ("0", "false", "no")
# Сколько секунд после отправки хэш держит режим ПОЛНОГО чтения блоков. 30с = ~15 блоков:
# принятые пробы садились и с задержкой +9 блоков (замер j2), поэтому окно полного чтения
# обязано перекрывать эту задержку, иначе «absent» врёт.
CHAIN_INDEX_WANT_TTL_SEC = float(os.environ.get("CHAIN_INDEX_WANT_TTL_SEC", "30"))
# А ПОМНИТЬ про заявленный хэш надо ДОЛЬШЕ, чем греть блоки. Это разные величины, и путать их
# нельзя: приговор «tx не села» выносится в `_engine_timeout`, то есть через
# SPEC_FIRE_ENGINE_COLLECT_TIMEOUT_SEC=180с после отправки. Если к этому моменту запись о
# хэше уже стёрта, `state()` честно отвечает «не регистрировался» = unknown, и весь смысл
# пакета (различать «сеть молчит» и «tx правда нет») пропадает ровно в точке применения.
# Класс дефекта известен: «гейт проверяли в МОМЕНТЕ, а не во ВРЕМЕНИ».
CHAIN_INDEX_WANT_KEEP_SEC = float(os.environ.get("CHAIN_INDEX_WANT_KEEP_SEC", "300"))
# СКОЛЬКО БЛОКОВ ПОДРЯД надо прочитать ЦЕЛИКОМ, чтобы иметь право сказать «tx не села».
# Величина связана с окном прогрева (WANT_TTL 30с ≈ 15 блоков) и обязана быть НЕ БОЛЬШЕ его:
# больше — и приговор не станет доступен НИКОГДА (мы просто перестанем читать блоки целиком).
# Меньшего окна тоже мало: принятые пробы садились с задержкой до +9 блоков (замер j2).
# Утверждение получается ограниченным намеренно — «в первых N блоках после отправки её нет».
# Оно применяется В КОНЪЮНКЦИИ с проверкой nonce на цепи (`volley_leg_landed`), которая ловит
# посадку ПОЗЖЕ окна: вместе это и есть приговор, поодиночке — догадки.
CHAIN_INDEX_ABSENT_BLOCKS = int(os.environ.get("CHAIN_INDEX_ABSENT_BLOCKS", "12"))
# Читать ли блок ЦЕЛИКОМ, когда интересный лог есть, но НАШИХ проб в полёте нет. Это цена
# наблюдаемости чужих гонок, и она ЗАМЕРЕНА (13.08, живой прогон 60с + 5 одиночных блоков):
#   * доля полных чтений — 5 из 30 блоков (17%; отдельный замер на 20 блоках дал 3/20 = 15%);
#   * полный блок ПО ПРОВОДУ с gzip — 55/70/109/59/56 КБ (среднее 70 КБ), сырой JSON 0.33-0.64 МБ;
#   * пустой getLogs по проводу — 37 байт.
# Итого ~7200 полных чтений/сут x 70 КБ + 43200 getLogs = **~0.5 ГБ/сутки**, а не «8 ГБ» из
# прежней редакции: та цифра брала 1.2 МБ на блок (завышено вдвое) и вовсе игнорировала gzip,
# который requests просит по умолчанию. Взамен мы видим tip транзита и позицию победителя в
# КАЖДОЙ чужой гонке, а без этого A1v2 (пофидный transit-tip) слеп.
# =0 оставляет полное чтение ТОЛЬКО на блоки, где летят наши пробы: трафик падает до нуля в
# штиль, но чужие гонки перестают измеряться.
CHAIN_INDEX_FULL_ON_LOGS = os.environ.get(
    "CHAIN_INDEX_FULL_ON_LOGS", "1").lower() not in ("0", "false", "no", "off")
# Прореживание громких строк (отказ узла / протухание): без него лежащий узел даёт строку/сек.
CHAIN_INDEX_LOUD_SEC = float(os.environ.get("CHAIN_INDEX_LOUD_SEC", "60"))
# Призовое окно позиций (Ш10): наша tx считается «в полосе», если она в [T+1, T+PRIZE] за транзитом.
CHAIN_INDEX_PRIZE_WINDOW = int(os.environ.get("CHAIN_INDEX_PRIZE_WINDOW", "20"))
# ПОТОЛОК СКАНИРОВАНИЯ. Подача блоков идёт из flashblocks, а это номер ФОРМИРУЕМОГО блока
# (pending): узел его ещё не отдаёт. Живой замер 13.08 на двух публичных узлах Base
# (mainnet.base.org, base-rpc.publicnode.com): eth_getLogs на head+1 отвечает
# «-32602 block range extends beyond current head block», eth_getBlockByNumber — result:null.
# Поэтому подача = БУДИЛЬНИК, а потолок = ПОДТВЕРЖДЁННАЯ УЗЛОМ голова (eth_blockNumber,
# кэш на HEAD_TTL). Узел не ответил про голову — потолок берём как hint-HINT_LAG (подача несёт
# формируемый блок, значит предыдущий запечатан). Без потолка первый же скан падал, `_last_bn`
# оставался None, и `_catch_up` вечно бился в недоступную голову: ВЕЧНЫЙ ЛАТЧ на нулевом
# покрытии — «блоков=0» при живом треде.
CHAIN_INDEX_HEAD_TTL_SEC = float(os.environ.get("CHAIN_INDEX_HEAD_TTL_SEC", "2.0"))
CHAIN_INDEX_HINT_LAG = int(os.environ.get("CHAIN_INDEX_HINT_LAG", "1"))
# Сколько оборотов подряд один и тот же номер может считаться «ещё не запечатанным», прежде чем
# это объявляется ЗАЛИПАНИЕМ вслух. «Не отказ» не имеет права быть тихим навсегда.
CHAIN_INDEX_BEYOND_LOUD_N = int(os.environ.get("CHAIN_INDEX_BEYOND_LOUD_N", "15"))
# Срок годности факта «tx замайнена» (реорг). На Base секвенсерных реоргов мы не наблюдали, и
# основную защиту даёт ПЕРЕЧИТЫВАНИЕ блока (повторный скан того же номера вычищает прежние
# факты этого блока), но вечного «mined» быть не должно: факт старше срока = «не знаю».
# Значение по умолчанию заведомо больше таймаута коллектора (180с), иначе гейт превратится в
# генератор ложных приговоров.
CHAIN_INDEX_MINED_TTL_SEC = float(os.environ.get("CHAIN_INDEX_MINED_TTL_SEC", "600"))
# Приёмочная строка на старте (пункт «а»): через сколько секунд после подъёма индекс сам
# докладывает, ЧИТАЕТ ли он цепь. Молчание не должно быть неотличимо от «всё хорошо».
CHAIN_INDEX_SELFCHECK_SEC = float(os.environ.get("CHAIN_INDEX_SELFCHECK_SEC", "60"))


class IndexUnavailable(RuntimeError):
    """Узел не ответил. Это «не знаю», а НЕ «в блоке пусто» (урок #101: различие обязано быть
    в ТИПЕ исхода). Секрет наружу не выносим: в тексте только хост, ключ живёт в пути URL."""

    def __init__(self, reason: str, host: str = ""):
        self.reason = str(reason)[:120]
        self.host = host or "?"
        super().__init__("%s: %s" % (self.host, self.reason))


class IndexBeyondHead(IndexUnavailable):
    """«Этого блока у узла ЕЩЁ нет» — отдельный тип, потому что это НЕ отказ.

    Считать его отказом означало ровно то, что и случилось в проводке: подача из flashblocks
    несёт pending-номер, первый же скан падает, `_last_bn` не двигается НИКОГДА, а журнал
    заполняется ERROR о живом узле. Здесь: счётчик растёт, `_last_bn` не замораживается,
    ERROR не пишется, потолок объявляется протухшим и переспрашивается.
    """


def _is_beyond_head(code, msg) -> bool:
    """PURE: ответ узла означает «блок ещё не запечатан», а не «узел лёг»?

    Тексты сверены живьём (13.08): Base отвечает «block range extends beyond current head
    block: requested N, head M» с кодом -32602; geth-семейство на неизвестный блок —
    «header not found» / «unknown block»; eth_getBlockByNumber на будущий блок — result:null
    (это разбирается отдельно, у null нет кода).
    """
    m = str(msg or "").lower()
    if "beyond" in m and "head" in m:
        return True
    for s in ("header not found", "unknown block", "block not found", "future block",
              "requested block is beyond", "cannot query unfinalized"):
        if s in m:
            return True
    return code == -32602 and "head" in m


def _host(url: str) -> str:
    try:
        return str(url).split("//")[-1].split("/")[0] or "?"
    except Exception:
        return "?"


# ---------------------------------------------------------------- чистый разбор
def _hexint(v, default=0):
    """'0x..' | int | None -> int. Нечитаемое = default (fail-open: не знаем, а не «ноль»)."""
    if v is None:
        return default
    if isinstance(v, int):
        return v
    try:
        s = str(v)
        return int(s, 16) if s.startswith("0x") else int(s)
    except Exception:
        return default


def _topic_hex(t) -> str:
    """topics приходят HexBytes (web3) либо строкой (сырой JSON-RPC) — приводим к '0x…'."""
    if isinstance(t, (bytes, bytearray)):
        return "0x" + bytes(t).hex()
    s = str(t)
    return (s if s.startswith("0x") else "0x" + s).lower()


def _data_words(lg) -> list:
    """data лога -> список uint256. Нечитаемое = [] (числа станут нулями = «не режем»)."""
    d = lg.get("data") if isinstance(lg, dict) else getattr(lg, "data", None)
    if d is None:
        return []
    if isinstance(d, (bytes, bytearray)):
        raw = bytes(d)
    else:
        s = str(d)
        s = s[2:] if s.startswith("0x") else s
        try:
            raw = bytes.fromhex(s)
        except ValueError:
            return []
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw) - 31, 32)]


def parse_logs(logs, morpho=MORPHO_BLUE) -> dict:
    """PURE: сырой ответ eth_getLogs -> {"transit": [...], "liquidate": [...], "pairs": {...}}.

    ФИЛЬТР ЭМИТЕНТА. Запрос идёт topics-OR без address (иначе понадобилось бы ДВА getLogs:
    AnswerUpdated шлют агрегаторы, Liquidate — Morpho), поэтому адрес проверяется здесь.
    Топик Liquidate не уникален для Morpho — любой контракт вправе эмитить событие с той же
    сигнатурой, и чужой лог отравил бы и вердикт победы, и фильтр трупов #108 (цель считалась
    бы убитой). Отброшенные логи считаются (`liquidate_foreign`), а не исчезают молча.

    transit:   [{"tx","agg","log_index"}]         — AnswerUpdated: КТО обновил цену;
    liquidate: [{"tx","mid","caller","borrower","repaid_shares","bad_debt_shares"}];
    pairs:     {(mid, borrower): (repaid_shares, bad_debt_shares)} — сырьё вердикта трупа (#108).

    Порядок полей data Liquidate — repaidAssets, repaidShares, seizedAssets, badDebtAssets,
    badDebtShares (сверен на цепи, 36/36). Ключ пары ПАРНЫЙ: позиция в Morpho живёт по рынку,
    и заёмщик, убитый в рынке A, остаётся законной целью в рынке B.
    """
    out = {"transit": [], "liquidate": [], "pairs": {}, "liquidate_foreign": 0}
    _mb = str(morpho or "").lower()
    for lg in logs or []:
        try:
            tops = lg.get("topics") if isinstance(lg, dict) else getattr(lg, "topics", None)
            if not tops:
                continue
            t0 = _topic_hex(tops[0])
            txh = str((lg.get("transactionHash") if isinstance(lg, dict)
                       else getattr(lg, "transactionHash", "")) or "").lower()
            if t0 == ANSWER_UPDATED_TOPIC:
                out["transit"].append({"tx": txh,
                                       "agg": str(lg.get("address") or "").lower(),
                                       "log_index": _hexint(lg.get("logIndex"), 0)})
            elif t0 == MORPHO_LIQUIDATE_TOPIC and len(tops) >= 4:
                if _mb and str(lg.get("address") or "").lower() != _mb:
                    out["liquidate_foreign"] += 1
                    continue
                w = _data_words(lg)
                mid = _topic_hex(tops[1])
                caller = "0x" + _topic_hex(tops[2])[-40:]
                borrower = "0x" + _topic_hex(tops[3])[-40:]
                rep = w[1] if len(w) > 1 else 0
                bad = w[4] if len(w) > 4 else 0
                out["liquidate"].append({"tx": txh, "mid": mid, "caller": caller,
                                         "borrower": borrower, "repaid_shares": rep,
                                         "bad_debt_shares": bad})
                prev = out["pairs"].get((mid, borrower))
                if prev is None:
                    out["pairs"][(mid, borrower)] = (rep, bad)
                else:
                    out["pairs"][(mid, borrower)] = (prev[0] + rep, max(prev[1], bad))
        except Exception:
            continue                     # один битый лог не смеет уронить разбор блока
    return out


def tx_tip(tx, base_fee=0) -> int:
    """Приоритетная цена tx в wei. type-2 несёт maxPriorityFeePerGas; legacy — gasPrice-base."""
    mp = tx.get("maxPriorityFeePerGas")
    if mp is not None:
        return _hexint(mp, 0)
    gp = _hexint(tx.get("gasPrice"), 0)
    return max(0, gp - int(base_fee or 0))


def tx_eff_gas_price(tx, base_fee) -> int:
    """effectiveGasPrice БЕЗ квитанции: base + min(tip, maxFee - base) (EIP-1559, ровно то же
    число, что кладёт в квитанцию узел). Единственное поле квитанции, которого так НЕ получить,
    — gasUsed, и мы его не выдумываем: у факта оно остаётся None."""
    mp = tx.get("maxPriorityFeePerGas")
    if mp is None:
        return _hexint(tx.get("gasPrice"), 0)
    bf = int(base_fee or 0)
    tip = _hexint(mp, 0)
    mx = _hexint(tx.get("maxFeePerGas"), 0)
    return bf + max(0, min(tip, mx - bf))


def parse_block(blk, watch_to=(), want_hashes=()) -> dict:
    """PURE: сырой ответ eth_getBlockByNumber(fullTx=true) -> факты блока.

    Держим в памяти НЕ весь блок (1175 tx × 64 блока сожрут память и ядро), а только:
      * tx, адресованные нашим контрактам (`watch_to` — доска и ликвидатор);
      * tx, на которые сослались интересные логи (`want_hashes` — транзит и победитель).
    Позиция берётся из ПОЛЯ transactionIndex, а не из места в списке: так факт остаётся верным
    и на урезанной выборке, и на ответе, где узел отдал tx не по порядку.
    """
    if not isinstance(blk, dict):
        return {}
    txs = blk.get("transactions") or []
    base_fee = _hexint(blk.get("baseFeePerGas"), 0)
    bn = _hexint(blk.get("number"), 0)
    watch = {str(a).lower() for a in (watch_to or ()) if a}
    want = {str(h).lower() for h in (want_hashes or ()) if h}
    ours, other, tips = {}, {}, {}
    for tx in txs:
        if not isinstance(tx, dict):
            continue                      # fullTx=false отдаёт голые хэши — позиции есть, tip нет
        h = str(tx.get("hash") or "").lower()
        idx = _hexint(tx.get("transactionIndex"), -1)
        tip = tx_tip(tx, base_fee)
        tips[idx] = tip
        to = str(tx.get("to") or "").lower()
        if to in watch or h in want:
            fact = {"hash": h, "block": bn, "index": idx, "tip": tip,
                    "max_fee": _hexint(tx.get("maxFeePerGas"), 0),
                    "eff_gas_price": tx_eff_gas_price(tx, base_fee),
                    "gas_limit": _hexint(tx.get("gas"), 0),
                    "from": str(tx.get("from") or "").lower(), "to": to,
                    "nonce": _hexint(tx.get("nonce"), 0),
                    "status": None, "gas_used": None, "logs": None, "won": None,
                    "src": "block"}
            (ours if to in watch else other)[h] = fact
    return {"block": bn, "ts": _hexint(blk.get("timestamp"), 0), "base_fee": base_fee,
            "ntx": len(txs), "ours": ours, "other": other, "tips": tips}


def band_ahead(tips: dict, start_idx: int) -> int:
    """ПРИЗОВАЯ ПОЛОСА ВПЕРЁД: сколько подряд идущих tx НАЧИНАЯ С транзита несут тот же tip.

    Это НЕ вся полоса ничьей — это её часть за спиной транзита, то есть ровно те позиции, за
    которые идёт драка (победитель стоит на транзит+1). Прежнее имя `tie_band` читалось как
    «вся ничья» и занижало картину вдвое: на эталоне 49890266 вперёд от 1083 идут 37 tx, а
    ВСЯ полоса того же tip — 1035..1119, то есть 85. Обе величины нужны и разведены именами:
    вперёд — размер приза, вся полоса — сколько народу стоит на этой цене вообще.
    """
    if start_idx is None or start_idx < 0:
        return 0
    t = tips.get(start_idx)
    if t is None:
        return 0
    n, i = 0, start_idx
    while tips.get(i) == t:
        n += 1
        i += 1
    return n


def band_full(tips: dict, idx: int):
    """ВСЯ полоса одного tip вокруг позиции: (первая позиция, длина). Эталон 49890266:
    (1035, 85) вокруг транзита 1083 — против 37 «вперёд»."""
    if idx is None or idx < 0:
        return (idx, 0)
    t = tips.get(idx)
    if t is None:
        return (idx, 0)
    lo = idx
    while tips.get(lo - 1) == t:
        lo -= 1
    hi = idx
    while tips.get(hi) == t:
        hi += 1
    return (lo, hi - lo)


def build_facts(blk, logs, watch_to=(), receipts=None) -> dict:
    """PURE: блок + логи (+ опционально квитанции блока) -> запись индекса за этот блок.

    ЭТО И ЕСТЬ настоящая функция индекса: тесты гоняют её на ЗАПИСАННОМ ответе узла, подменяя
    только сокет. Вердикт победы выносится ПЕРЕСЕЧЕНИЕМ: наш хэш встретился среди логов
    Liquidate у Morpho. Обратное («в блоке нет нашего хэша среди Liquidate») тоже утверждение,
    а не молчание: победа обязана эмитить это событие, поэтому won=False здесь — ФАКТ, а не
    отсутствие данных. Логи не прочитаны -> won остаётся None.
    """
    parsed = parse_logs(logs) if logs is not None else None
    want = set()
    if parsed:
        want |= {r["tx"] for r in parsed["transit"] if r["tx"]}
        want |= {r["tx"] for r in parsed["liquidate"] if r["tx"]}
    f = parse_block(blk, watch_to=watch_to, want_hashes=want) if blk else {}
    if not f:
        return {}
    f["logs_ok"] = parsed is not None
    f["pairs"] = (parsed or {}).get("pairs", {})
    tips = f.get("tips") or {}
    by_hash = dict(f.get("ours") or {})
    by_hash.update(f.get("other") or {})
    transit = []
    for r in (parsed or {}).get("transit", []):
        t = by_hash.get(r["tx"])
        if t is not None:
            _lo, _all = band_full(tips, t["index"])
            transit.append({"index": t["index"], "tip": t["tip"], "agg": r["agg"], "tx": r["tx"],
                            "band_ahead": band_ahead(tips, t["index"]),
                            "band_full": _all, "band_from": _lo})
    winners = []
    for r in (parsed or {}).get("liquidate", []):
        t = by_hash.get(r["tx"])
        winners.append({"index": (t or {}).get("index", -1), "tip": (t or {}).get("tip", -1),
                        "caller": r["caller"], "mid": r["mid"], "borrower": r["borrower"],
                        "tx": r["tx"]})
    f["transit"] = sorted(transit, key=lambda x: x["index"])
    f["winners"] = sorted(winners, key=lambda x: x["index"])
    won_hashes = {r["tx"] for r in (parsed or {}).get("liquidate", [])}
    for h, fact in (f.get("ours") or {}).items():
        if parsed is not None:
            fact["won"] = h in won_hashes
    if receipts:
        _merge_receipts(f, receipts)
    f.pop("tips", None)                  # позиции нужны, а 1175 чисел на блок держать незачем
    return f


def _merge_receipts(f: dict, receipts) -> None:
    """Квитанции блока (eth_getBlockReceipts) -> статус/газ/логи НАШИХ tx. Чужие не храним."""
    ours = f.get("ours") or {}
    n = 0
    for r in receipts or []:
        try:
            h = str(r.get("transactionHash") or "").lower()
            fact = ours.get(h)
            if fact is None:
                continue
            fact["status"] = _hexint(r.get("status"), None)
            fact["gas_used"] = _hexint(r.get("gasUsed"), None)
            if r.get("effectiveGasPrice") is not None:
                fact["eff_gas_price"] = _hexint(r.get("effectiveGasPrice"), fact["eff_gas_price"])
            fact["logs"] = r.get("logs") or []
            fact["src"] = "block+receipts"
            n += 1
        except Exception:
            continue
    f["receipts_merged"] = n


def rcpt_int(v, default=0):
    """Поле квитанции -> int, ОДИНАКОВО для обоих источников.

    Сеть отдаёт '0x...' (сырой JSON-RPC через `batch_receipts`), индекс держит числа. Пока
    единого разбора не было, `int(r["gasUsed"])` на сетевой квитанции падал с ValueError,
    ветка глоталась общим `except` и денежный учёт пробы МОЛЧА обнулялся — то есть включение
    индекса (числа вместо hex) само по себе меняло поведение суточного kill-switch. Формат
    теперь один (см. `receipt_like`), а разбор — вот этот.
    """
    return _hexint(v, default)


def receipt_like(fact) -> dict:
    """Факт индекса -> квитанция В ТОЙ ЖЕ ФОРМЕ, что отдаёт узел (hex-строки).

    ОДИН ФОРМАТ НА ОБА ИСТОЧНИКА — намеренно. Потребитель (`_engine_result`) не должен знать,
    откуда приехала квитанция: любое различие формы здесь превращается в тихую развилку
    поведения там. Второй формат уже стоил нам молчаливого ValueError в денежном пути.

    Отдаётся ТОЛЬКО когда газ известен по-настоящему (пришли квитанции блока). Без gasUsed
    исход выдавать нельзя: денежный учёт по выдуманному числу — это вторая ложь вместо первой.
    """
    if not fact or fact.get("gas_used") is None or fact.get("status") is None:
        return None
    return {"status": hex(int(fact["status"])), "gasUsed": hex(int(fact["gas_used"])),
            "effectiveGasPrice": hex(int(fact.get("eff_gas_price") or 0)),
            "logs": fact.get("logs") or [], "blockNumber": hex(int(fact.get("block") or 0)),
            "transactionIndex": hex(int(fact.get("index") or 0)), "from": fact.get("from"),
            "to": fact.get("to"), "_index_src": fact.get("src", "block+receipts")}


# ---------------------------------------------------------------- транспорт
_SESSIONS: dict = {}
_SESS_LOCK = threading.Lock()


def _session(url: str):
    try:
        import requests
    except Exception:
        return None
    with _SESS_LOCK:
        s = _SESSIONS.get(url)
        if s is None:
            s = requests.Session()
            # Пул НАМЕРЕННО крошечный: индекс — один тред с последовательными запросами, а
            # лишние коннекты на одноядерной машине это чистый эффект наблюдателя.
            ad = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4)
            s.mount("https://", ad)
            s.mount("http://", ad)
            # Публичные узлы Base отбивают дефолтный UA python-requests (в репо это уже
            # закреплено для аналитики: analysis/build_covered_markets.py). Индексу разрешено
            # ехать на публичный узел как на запасной, поэтому UA ставим сразу.
            s.headers.update({"User-Agent": os.environ.get("CHAIN_INDEX_UA", "curl/8.5.0"),
                              "Content-Type": "application/json"})
            _SESSIONS[url] = s
        return s


def http_call(url, payload, timeout=CHAIN_INDEX_TIMEOUT):
    """Один JSON-RPC POST. Отказ транспорта = IndexUnavailable (не пустой ответ!)."""
    s = _session(url)
    if s is None:
        raise IndexUnavailable("нет http-сессии (requests недоступен)", _host(url))
    try:
        return s.post(url, json=payload, timeout=timeout).json()
    except Exception as e:
        raise IndexUnavailable("транспорт %s" % type(e).__name__, _host(url)) from None


# ---------------------------------------------------------------- сам индекс
class ChainIndex:
    """Фоновый индекс цепи. ОДИН тред, очередь глубиной в один блок (голова вытесняет хвост).

    Швы: `http` (единственный сетевой стык — его и подменяет тест), `now`, `sleep`.
    Всё остальное — настоящие функции: разбор блока, разбор логов, склейка квитанций,
    учёт покрытия и вердикты. Шов стоит НИЖЕ дефекта намеренно (урок «шов подменён выше
    дефекта»: 38 зелёных тестов при 100% мёртвом боевом пути).
    """

    def __init__(self, url, watch_to=(), http=None, log_fn=None, now=None, sleep=None,
                 keep_blocks=None, receipts=None, prize_window=None):
        self.url = url
        self._http = http or http_call
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._keep = int(keep_blocks or CHAIN_INDEX_KEEP_BLOCKS)
        self._prize = int(prize_window if prize_window is not None else CHAIN_INDEX_PRIZE_WINDOW)
        self._watch = {str(a).lower() for a in (watch_to or ()) if a}
        self._receipts_ok = (CHAIN_INDEX_RECEIPTS if receipts is None else bool(receipts))
        self._lock = threading.RLock()
        self._blocks = {}          # bn -> facts
        self._by_hash = {}         # tx hash -> fact
        self._seen = {}            # bn -> full?(bool) — покрытие, включая «пустые» блоки
        self._pairs = {}           # (mid, borrower) -> (repaid, bad, block)
        self._wanted = {}          # наш хэш -> (mono отправки, ЗАПЕЧАТАННАЯ голова на тот момент)
        self._wlock = threading.RLock()   # только _wanted/_hot_until: горячий note_hash не
        #                                   имеет права ждать лока, под которым разбирается блок
        self._hot_until = 0.0      # до какого mono греем блоки целиком (O(1) вместо обхода)
        self._head = 0             # последний ПОДАННЫЙ снаружи номер (БУДИЛЬНИК, не потолок!)
        self._head_conf = 0        # ПОДТВЕРЖДЁННАЯ узлом голова (eth_blockNumber)
        self._head_conf_ts = 0.0
        self._last_bn = None       # последний просканированный
        self._last_ok = 0.0        # mono последнего успешного скана
        self._t0 = self._now()
        self._selfchecked = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thr = None
        self._loud_ts = 0.0
        self._fail_streak = 0
        self._beyond_bn = None     # на каком номере залипли (см. CHAIN_INDEX_BEYOND_LOUD_N)
        self._beyond_n = 0
        self.stats = {"blocks": 0, "full": 0, "light": 0, "logs": 0, "receipts": 0,
                      "gaps": 0, "fails": 0, "shed": 0, "ours": 0, "transit": 0,
                      "winners": 0, "self_polls": 0, "stale_events": 0,
                      "beyond_head": 0, "head_polls": 0, "rpc_calls": 0, "cu": 0.0,
                      "reingest": 0, "foreign_liq": 0}

    # ---- реестр интереса ----
    def watch_contract(self, addr):
        if addr:
            with self._lock:
                self._watch.add(str(addr).lower())

    def sealed_head(self):
        """Последний блок, про который ТОЧНО известно, что он запечатан.

        `self._head` сюда НЕ входит: подача идёт из flashblocks и несёт ФОРМИРУЕМЫЙ блок.
        Взять его за «голову на момент отправки» значило бы объявить запечатанным блок, в
        который наша проба как раз и метит. Оба слагаемых ниже — доказанные факты: прочитанный
        блок существует, подтверждённая голова получена от узла.
        """
        return max(self._last_bn or 0, self._head_conf or 0)

    def note_hash(self, h, head=None):
        """Зарегистрировать НАШ хэш. Вызывается С ГОРЯЧЕГО ПУТИ ОТПРАВКИ, поэтому здесь только
        свой маленький лок и присвоения — ни обхода словаря, ни лока разбора блока (прежняя
        редакция брала общий лок и обходила `_wanted` в `_want_full`: до ~12мс на одноядерной
        VPS ровно в момент залпа).

        Запоминаем ЗАПЕЧАТАННУЮ голову на момент отправки: раньше неё tx оказаться не может, и
        именно от неё (+1) считается право сказать «absent».
        """
        if not h:
            return
        t = self._now()
        with self._wlock:
            k = str(h).lower()
            if k not in self._wanted:
                self._wanted[k] = (t, int(head if head is not None else self.sealed_head()))
            if t + CHAIN_INDEX_WANT_TTL_SEC > self._hot_until:
                self._hot_until = t + CHAIN_INDEX_WANT_TTL_SEC
        self._wake.set()

    def _want_full(self):
        """Греем ли блоки целиком. O(1): держим ГРАНИЦУ прогрева, а не обходим заявки.
        Забываем заявку ПОЗЖЕ (KEEP), чем греем (TTL) — приговор по ней выносится через 180с,
        и стёртая раньше запись обесценила бы весь пакет; чистка — в `_prune_wanted` раз в блок.
        """
        return self._now() < self._hot_until

    def _prune_wanted(self):
        """Выселение старых заявок. Раз в блок (из `_trim`), а не на каждый вызов гейта."""
        t = self._now()
        with self._wlock:
            for h, (ts, _bn) in list(self._wanted.items()):
                if t - ts > CHAIN_INDEX_WANT_KEEP_SEC:
                    self._wanted.pop(h, None)

    # ---- подача блоков ----
    def offer_block(self, bn):
        """Вызывается из recv-петли: только присвоение и Event.set (сети нет, лока нет)."""
        try:
            bn = int(bn)
        except Exception:
            return
        if bn > self._head:
            self._head = bn
            self._wake.set()

    # ---- жизненный цикл ----
    def start(self):
        if self._thr is not None:
            return self
        self._stop.clear()
        self._thr = threading.Thread(target=self._loop, daemon=True, name="chain-index")
        self._thr.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def alive(self):
        t = self._thr
        return t is not None and t.is_alive()

    # ---- чтение (потребители) ----
    def age(self):
        """Возраст последнего успешного скана в секундах. Никогда не сканировали -> +inf."""
        if not self._last_ok:
            return float("inf")
        return max(0.0, self._now() - self._last_ok)

    def fresh(self):
        return self.age() <= CHAIN_INDEX_STALE_SEC

    def lookup(self, h):
        with self._lock:
            return self._by_hash.get(str(h).lower())

    def state(self, h, sent_ts=None):
        """('mined', факт) | ('absent', None) | ('unknown', причина).

        'absent' — САМОЕ сильное утверждение здесь, и выдаётся оно только по ФАКТУ покрытия:
        индекс свеж и все блоки от головы-на-момент-отправки и ещё N подряд прочитаны ЦЕЛИКОМ
        (`_seen[bn] is True`). Дыра, лёгкое чтение или незакрытое окно дают 'unknown', и
        потребитель обязан вести себя ровно как до пакета. Именно эта развилка закрывает ложь
        «NO-RECEIPT ⇒ проба не села», из-за которой сбрасывались склады живых ног.

        Границу утверждения называем честно: это «её нет в первых N блоках после отправки», а
        не «её нет в цепи вовсе». Посадку ПОЗЖЕ окна ловит сверка nonce на цепи, и вызывающий
        (`_engine_timeout`) применяет обе проверки подряд — см. комментарий у ABSENT_BLOCKS.
        """
        key = str(h).lower()
        now = self._now()
        with self._lock:
            fact = self._by_hash.get(key)
            if fact is not None:
                # Срок годности у ФАКТА: реорг мы не отслеживаем, поэтому вечного «mined» быть
                # не должно. На практике раньше срабатывает кольцо блоков, а перечитывание того
                # же номера вычищает прежние факты — это основная защита, TTL лишь потолок.
                _age = now - float(fact.get("ts") or 0.0)
                if CHAIN_INDEX_MINED_TTL_SEC > 0 and _age > CHAIN_INDEX_MINED_TTL_SEC:
                    return ("unknown", "факт о посадке протух (%.0fс > %.0fс, реорг не отслеживаем)"
                            % (_age, CHAIN_INDEX_MINED_TTL_SEC))
                return ("mined", fact)
            if not self.fresh():
                return ("unknown", "индекс протух (%.1fс)" % self.age())
            with self._wlock:
                want = self._wanted.get(key)
            from_bn = want[1] if want else None
            last = self._last_bn
            if want is None:
                return ("unknown", "хэш не регистрировался в индексе")
            if not from_bn or last is None:
                # Отдельная причина, а не общая: «мы не знали запечатанной головы в момент
                # отправки» — это про НАС, а не про хэш, и в журнале это разные диагнозы.
                return ("unknown", "на момент отправки индекс не знал запечатанной головы цепи")
            # ОКНО НАЧИНАЕТСЯ С from_bn+1. from_bn — блок, ЗАПЕЧАТАННЫЙ на момент отправки:
            # наша tx в нём оказаться не может по построению, а прочитан он был ДО регистрации
            # хэша, то есть скорее всего ЛЕГКО (_seen=False). Требовать его полного покрытия
            # значило бы никогда не выдавать «absent» — счётчик noreceipt_dropped_confirmed
            # был структурно нулевым, а «мы правда теряем ноги» — недоказуемым.
            start = from_bn + 1
            upto = from_bn + max(1, CHAIN_INDEX_ABSENT_BLOCKS)
            if last < upto:
                return ("unknown", "окно наблюдения ещё не закрыто (%d из %d блоков)"
                        % (max(0, last - from_bn), CHAIN_INDEX_ABSENT_BLOCKS))
            for bn in range(start, upto + 1):
                if not self._seen.get(bn):
                    return ("unknown", "блок %d прочитан не полностью (или пропущен)" % bn)
        return ("absent", None)

    def block_covered(self, bn):
        """Прочитан ли блок ЦЕЛИКОМ. Третий исход для метрики позиций: «мы туда не смотрели»
        обязано отличаться от «транзита в блоке не было» (иначе слепота считается фактом)."""
        with self._lock:
            return self._seen.get(int(bn)) is True

    def receipts_for(self, hashes):
        """{хэш: квитанция-подобный словарь} для тех, кого индекс уже знает С ГАЗОМ.

        Ключей меньше, чем запрошено — это НОРМА: «остальных не знаю», а не «их нет».
        Отсутствие ключа отправляет хэш на прежний поштучный путь, а не в приговор.
        """
        out = {}
        with self._lock:
            for h in hashes or []:
                f = self._by_hash.get(str(h).lower())
                if f is None:
                    continue
                r = receipt_like(f)
                if r is not None:
                    out[h] = r
        return out

    def block_facts(self, bn):
        with self._lock:
            return self._blocks.get(int(bn))

    def transits_of(self, bn):
        """ВСЕ транзиты блока: [{"index","tip","agg","tx","band_ahead","band_full"}]. Их бывает несколько — в
        эталонном 49890266 их два (позиции 406 и 1083), и оба несут tip 2 000 000."""
        f = self.block_facts(bn)
        return list((f or {}).get("transit") or [])

    def transit_report(self, bn, agg=None):
        """(исход, транзит) — ЧЕТЫРЕ исхода, а не два:

          'uncovered'  — блок индексом целиком не прочитан. Судить не о чем: это СЛЕПОТА.
          'no_transit' — блок прочитан целиком, AnswerUpdated в нём нет. Это ФАКТ.
          'foreign'    — транзиты есть, но НАШЕГО агрегатора среди них нет.
          'transit'    — нашли (или, при неизвестном agg, выбрали призовой эвристикой).

        Зачем разведено. Единственный боевой вызов ходил сюда БЕЗ agg, и в блоке без Liquidate
        (а это подавляющее большинство блоков) метрика №1 считалась против ЧУЖОГО транзита:
        полоса и tip брались у постороннего события, и «в полосе=0» ничего не значило.
        """
        if not self.block_covered(bn):
            return ("uncovered", None)
        trs = self.transits_of(bn)
        if not trs:
            return ("no_transit", None)
        if agg:
            a = str(agg).lower()
            t = next((x for x in trs if x.get("agg") == a), None)
            return ("transit", t) if t is not None else ("foreign", None)
        # agg неизвестен: призовым считаем транзит, непосредственно предшествующий первой
        # ликвидации (за ним стоит победитель: транзит+1 в 8/8 разобранных блоков), иначе первый.
        wins = [w["index"] for w in self.winners_of(bn) if w.get("index", -1) >= 0]
        if wins:
            before = [x for x in trs if x["index"] < min(wins)]
            if before:
                return ("transit", before[-1])
        return ("transit", trs[0])

    def transit_of(self, bn, agg=None):
        """(index, tip, полоса ВПЕРЁД) призового транзита либо None. Тонкая обёртка над
        `transit_report` для мест, где различать слепоту и пустоту не нужно (отчёты)."""
        st, t = self.transit_report(bn, agg=agg)
        if st != "transit" or not t:
            return None
        return (t["index"], t["tip"], t.get("band_ahead", 0))

    def winners_of(self, bn):
        f = self.block_facts(bn)
        return list((f or {}).get("winners") or [])

    def corpses(self, max_age_blocks=None):
        """{(mid, borrower): (repaid_shares, bad_debt_shares)} за последние N блоков.

        ВЕРДИКТ здесь НЕ выносится: событие Liquidate само по себе трупа не доказывает (88.7%
        ликвидаций частичные). Числа отдаём потребителю (#108), он сверит их с остатком долга.
        Индекс протух -> None: «не знаю» обязано отличаться от «трупов не было», иначе фильтр
        молча выключится ровно тогда, когда цепь перестала читаться.
        """
        if not self.fresh():
            return None
        n = int(max_age_blocks or CHAIN_INDEX_CORPSE_BLOCKS)
        with self._lock:
            last = self._last_bn or 0
            return {k: (v[0], v[1]) for k, v in self._pairs.items() if v[2] >= last - n}

    def snapshot(self):
        with self._lock:
            s = dict(self.stats)
            _up = max(1e-9, self._now() - self._t0)
            s.update({"head": self._head, "last": self._last_bn or 0, "conf": self._head_conf,
                      "age": round(self.age(), 1), "fresh": self.fresh(),
                      "hashes": len(self._by_hash), "pairs": len(self._pairs),
                      "wanted": len(self._wanted), "receipts_cap": self._receipts_ok,
                      # ЗАМЕРЕННЫЙ темп, а не подразумеваемый: запросов/с и CU/сутки считаются
                      # от реального числа вызовов за реальный аптайм треда.
                      "rps": round(self.stats["rpc_calls"] / _up, 3),
                      "cu_day": int(self.stats["cu"] / _up * 86400)})
            return s

    # ---- позиции наших проб (метрика №1) ----
    def our_position_report(self, bn, agg=None):
        """Строка отчёта о позициях НАШИХ tx в блоке против транзита, либо None.

        Это и есть метрика №1 плана («наши пробы в [T+1, T+20] на tip ничьей»): раньше её
        нельзя было снять вовсе — квитанция даёт позицию, но не даёт ни tip транзита, ни
        ширины полосы ничьей, а полный блок никто не читал.
        """
        f = self.block_facts(bn)
        if not f:
            return None
        ours = f.get("ours") or {}
        if not ours:
            return None
        st, tr = self.transit_report(bn, agg=agg)
        idxs = sorted(x["index"] for x in ours.values())
        tips = sorted({x["tip"] for x in ours.values()})
        if st != "transit" or not tr:
            _why = {"uncovered": "БЛОК НЕ ПОКРЫТ индексом (судить не о чем)",
                    "no_transit": "транзита в блоке НЕТ",
                    "foreign": "транзиты есть, но НАШЕГО фида (agg=%s) среди них нет"
                               % (str(agg or "?")[:10],)}.get(st, st)
            return ("ПОЗИЦИИ blk=%d: наших %d (idx %d..%d), %s, tip %s"
                    % (bn, len(ours), idxs[0], idxs[-1], _why, "/".join(str(t) for t in tips)))
        band = [i for i in idxs if 1 <= i - tr["index"] <= self._prize]
        match = [x for x in ours.values() if x["tip"] == tr["tip"]]
        win = [x for x in ours.values() if x.get("won")]
        return ("ПОЗИЦИИ blk=%d: транзит idx=%d tip=%d agg=%s (полоса вперёд %d tx, вся полоса "
                "%d); наших %d, в полосе [T+1,T+%d]=%d, tip-match=%d, побед=%d; наши idx "
                "%d..%d tip %s"
                % (bn, tr["index"], tr["tip"], str(tr.get("agg") or "?")[:10],
                   tr.get("band_ahead", 0), tr.get("band_full", 0), len(ours), self._prize,
                   len(band), len(match), len(win), idxs[0], idxs[-1],
                   "/".join(str(t) for t in tips)))

    # ---- сканирование ----
    def _rpc(self, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        self.stats["rpc_calls"] += 1
        try:
            from chain import rpc as _rpc
            _rpc.rpc_usage_note(method, self.url, 1)
            # СВОЙ учёт CU рядом с общим: индекс ходит сырым POST мимо web3-обёртки, и «свой
            # счётчик слеп к необслуживаемому пути» нам уже стоил ложного вывода «расход не наш».
            self.stats["cu"] = self.stats.get("cu", 0.0) + float(_rpc.CU_COST.get(method, 20))
        except Exception:
            pass
        r = self._http(self.url, payload, CHAIN_INDEX_TIMEOUT)
        if not isinstance(r, dict):
            raise IndexUnavailable("ответ не JSON-RPC", _host(self.url))
        err = r.get("error")
        if err:
            code = (err or {}).get("code")
            msg = str((err or {}).get("message", ""))[:120]
            if _is_beyond_head(code, msg):
                raise IndexBeyondHead("%s %s" % (code, msg), _host(self.url))
            raise IndexUnavailable("%s %s" % (code, msg[:60]), _host(self.url))
        return r.get("result")

    def scan_block(self, bn, full=None):
        """ОДИН блок: логи -> (при надобности) полный блок -> (при надобности) квитанции блока.

        Возвращает факты блока либо {} (пустой блок без интереса). Отказ узла летит наружу
        IndexUnavailable — вызывающий пометит дыру, а не запишет «в блоке пусто».
        """
        bn = int(bn)
        if self._head_conf and bn > self._head_conf:
            # СТРУКТУРНЫЙ ГАРД, а не оптимизация: узлы отвечают на будущий блок по-разному —
            # один -32602, другой пустым списком (клампинг). Второй вариант молча пометил бы
            # несуществующий блок «прочитанным», и покрытие для «absent» стало бы враньём.
            # Тихая ветка хуже громкой: выше подтверждённой головы мы просто не ходим.
            raise IndexBeyondHead("блок %d выше подтверждённой головы %d" % (bn, self._head_conf),
                                  _host(self.url))
        logs = self._rpc("eth_getLogs", [{"fromBlock": hex(bn), "toBlock": hex(bn),
                                          "topics": [[ANSWER_UPDATED_TOPIC,
                                                      MORPHO_LIQUIDATE_TOPIC]]}])
        if logs is None:
            logs = []
        if not isinstance(logs, (list, tuple)):
            # Не список = ответ не тот, что мы просили. Молча читать это как «логов нет» нельзя:
            # ровно так фильтр трупов и вердикт победы превратились бы в вечное «пусто».
            raise IndexUnavailable("eth_getLogs отдал %s вместо списка" % type(logs).__name__,
                                   _host(self.url))
        self.stats["logs"] += 1
        parsed = parse_logs(logs)
        self.stats["foreign_liq"] += int(parsed.get("liquidate_foreign") or 0)
        interesting = bool(parsed["transit"] or parsed["liquidate"])
        need_full = bool(full)
        if full is None:
            need_full = (interesting and CHAIN_INDEX_FULL_ON_LOGS) or self._want_full()
        if not need_full:
            # Логи блока всё равно разобраны: трупы (#108) не зависят от полного чтения.
            self._ingest(bn, {}, full=False, pairs=parsed["pairs"])
            self.stats["light"] += 1
            return {}
        blk = self._rpc("eth_getBlockByNumber", [hex(bn), True])
        if not blk:
            # result:null — так публичные узлы Base отвечают на БУДУЩИЙ блок (проверено живьём).
            # Это «ещё нет», а не «узел лёг»: отказом считать нельзя, иначе снова вечный латч.
            if bn >= (self._head_conf or 0):
                raise IndexBeyondHead("узел отдал null на блок %d" % bn, _host(self.url))
            raise IndexUnavailable("узел не отдал блок %d" % bn, _host(self.url))
        with self._lock:
            watch = set(self._watch)
        facts = build_facts(blk, logs, watch_to=watch)
        rcpts = None
        if facts.get("ours") and self._receipts_ok:
            try:
                rcpts = self._rpc("eth_getBlockReceipts", [hex(bn)])
                self.stats["receipts"] += 1
            except IndexUnavailable as e:
                # -32601 = метод не поддержан узлом. Это СВОЙСТВО УЗЛА, а не сбой: гасим
                # способность навсегда и говорим об этом один раз, вместо вечных повторов.
                if "-32601" in str(e) or "unsupported" in str(e).lower() or "not supported" in str(e).lower():
                    self._receipts_ok = False
                    self._log("CHAIN-INDEX: узел=%s не умеет eth_getBlockReceipts — статус и "
                              "газ наших tx останутся за прежним трактом квитанций, позиции и "
                              "победы индекс даёт по-прежнему" % _host(self.url))
                else:
                    # Транзиентный отказ ИМЕННО квитанций не имеет права стоить нам блока:
                    # позиции и вердикт победы уже собраны из блока и логов. Считаем отказ,
                    # идём дальше без газа (у факта он останется None = «не знаю»).
                    self.stats["receipts_fail"] = self.stats.get("receipts_fail", 0) + 1
                    self._loud("CHAIN-INDEX: квитанции блока %d не прочитаны (%s) — позиции "
                               "есть, газ пойдёт прежним трактом" % (bn, str(e)[:80]), err=False)
            if rcpts:
                _merge_receipts(facts, rcpts)
        self.stats["full"] += 1
        self._ingest(bn, facts, full=True)
        return facts

    def _add_pairs(self, bn, pairs):
        """СУММИРУЕМ погашение по паре (рынок, заёмщик) между блоками, а не перезаписываем.

        Добой в два блока — обычное дело (88.7% ликвидаций частичные). Прежняя редакция клала
        последнее значение поверх, то есть ТЕРЯЛА раннее погашение: фильтр трупов #108 получил
        бы заниженное repaid и оставил бы уже мёртвую цель в работе. Под локом вызывающего.
        """
        for k, v in (pairs or {}).items():
            prev = self._pairs.get(k)
            if prev is None or prev[2] < bn - CHAIN_INDEX_CORPSE_BLOCKS:
                self._pairs[k] = (v[0], v[1], bn)
            else:
                self._pairs[k] = (prev[0] + v[0], max(prev[1], v[1]), max(prev[2], bn))

    def _ingest(self, bn, facts, full, pairs=None):
        """Положить факты блока в память и подвинуть учёт покрытия."""
        now = self._now()
        with self._lock:
            prev = self._last_bn
            if bn in self._blocks:
                # ПЕРЕЧИТЫВАНИЕ того же номера (реорг/повтор): прежние факты этого блока
                # вычищаем, иначе «mined» пережил бы исчезновение своей tx из канона.
                old = self._blocks.pop(bn, None) or {}
                for h in (old.get("ours") or {}):
                    self._by_hash.pop(h, None)
                self.stats["reingest"] += 1
            if prev is not None and bn > prev + 1:
                # ДЫРА. Отдельного флага ей не нужно: пропущенные блоки просто не появляются в
                # `_seen`, и `state()` спотыкается о них при проверке покрытия. Счётчик — ради
                # наблюдаемости (дыры = потерянные окна приговора).
                self.stats["gaps"] += 1
            # `_seen[bn] = False` для лёгкого чтения — это ЗАПИСЬ О НЕЗНАНИИ, а не о пустоте:
            # мы в этот блок за транзакциями не ходили и не имеем права утверждать, что нашей
            # там не было.
            self._seen[bn] = bool(full)
            if facts:
                self._blocks[bn] = facts
                with self._wlock:
                    for h, f in (facts.get("ours") or {}).items():
                        f["ts"] = now              # срок годности факта (реорг) считается отсюда
                        self._by_hash[h] = f
                        self._wanted.pop(h, None)
                self.stats["ours"] += len(facts.get("ours") or {})
                self.stats["transit"] += len(facts.get("transit") or [])
                self.stats["winners"] += len(facts.get("winners") or [])
                self._add_pairs(bn, facts.get("pairs") or {})
            self._add_pairs(bn, pairs or {})
            if bn > (prev or 0):
                self._last_bn = bn
            self._last_ok = now
            self.stats["blocks"] += 1
            self._trim()

    def _trim(self):
        """Кольцо: держим последние KEEP блоков. Под локом вызывающего."""
        last = self._last_bn or 0
        lo = last - self._keep
        for bn in [b for b in self._blocks if b < lo]:
            f = self._blocks.pop(bn, None)
            for h in (f.get("ours") or {}):
                self._by_hash.pop(h, None)
        for bn in [b for b in self._seen if b < last - max(self._keep, 256)]:
            self._seen.pop(bn, None)
        cl = last - CHAIN_INDEX_CORPSE_BLOCKS
        for k in [k for k, v in self._pairs.items() if v[2] < cl]:
            self._pairs.pop(k, None)
        self._prune_wanted()       # раз в блок, а не на каждый вызов гейта прогрева

    # ---- петля ----
    def _loud(self, msg, err=True):
        """Громко, но не в каждый оборот: лежащий узел иначе даёт строку в секунду."""
        now = self._now()
        if now - self._loud_ts < CHAIN_INDEX_LOUD_SEC:
            return
        self._loud_ts = now
        (log.error if err else log.warning)("%s", msg)

    def _shed(self) -> bool:
        """Уступить ли ключ бою. Сверяем СВОЙ url с ТЕКУЩИМ боевым указателем.

        Прежняя редакция звала `bg_shed()`, а он сравнивает боевой указатель с ЧУЖИМ адресом —
        тем, что записан в FailoverRpc как bg_url. Для индекса это ответ не про его ключ: он
        уступал, когда бой садился на чужой bg-ключ, и НЕ уступал, когда бой садился на наш.
        """
        try:
            from chain.failover import active_url
            a = _norm_url(active_url())
        except Exception:
            return False
        return bool(a) and a == _norm_url(self.url)

    def _confirmed_head(self, hint=0):
        """ПОТОЛОК сканирования: голова, которую узел признаёт. Кэш на HEAD_TTL.

        Дешевле, чем кажется: без потолка каждый оборот тратил бы eth_getLogs (75 CU) на
        заведомо недоступный pending-блок; eth_blockNumber стоит 10 CU и отвечает за ~2с
        (один блок Base). Узел про голову не ответил -> берём hint-HINT_LAG: подача несёт
        ФОРМИРУЕМЫЙ блок, значит предыдущий уже запечатан.
        """
        t = self._now()
        if self._head_conf and (t - self._head_conf_ts) <= CHAIN_INDEX_HEAD_TTL_SEC:
            return max(self._head_conf, self._last_bn or 0)
        try:
            top = _hexint(self._rpc("eth_blockNumber", []), 0)
            self.stats["head_polls"] += 1
            self.stats["self_polls"] += 1
            if top > 0:
                self._head_conf, self._head_conf_ts = top, t
                if self._fail_streak:
                    self._log("CHAIN-INDEX: узел снова отвечает (после %d отказов), голова %d"
                              % (self._fail_streak, top))
                self._fail_streak = 0
        except Exception as e:
            self._note_fail(e)
            top = self._head_conf or (int(hint or 0) - CHAIN_INDEX_HINT_LAG)
        return max(int(top or 0), self._last_bn or 0)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._wake.wait(timeout=CHAIN_INDEX_SELF_POLL_SEC)
                self._wake.clear()
                if self._stop.is_set():
                    break
                if self._shed():
                    # Бой уехал на НАШ ключ: фон обязан уступить. Индекс при этом ПРОТУХАЕТ,
                    # и потребитель это увидит (state -> unknown), а не получит пустоту.
                    self.stats["shed"] += 1
                    continue
                self._catch_up(self._head)
                self._selfcheck()
            except Exception as e:                      # петля не имеет права умереть молча
                self._note_fail(e)

    def _catch_up(self, hint=0):
        """Дочитать цепь ДО ПОДТВЕРЖДЁННОЙ УЗЛОМ головы. `hint` — только будильник.

        Здесь и лежал главный дефект проводки: подача из flashblocks несёт pending-номер,
        `start` при пустом `_last_bn` вставал на него же, скан падал (-32602, проверено живьём
        на двух публичных узлах), функция делала `return` — и `_last_bn` оставался None
        НАВСЕГДА. Живой тред, «блоков=0», ни одной прочитанной строки цепи.
        """
        top = self._confirmed_head(hint)
        if top <= 0:
            return
        last = self._last_bn
        start = (last + 1) if last is not None else top
        if start > top:
            return                    # узел ещё не запечатал ничего нового — это НЕ отказ
        if top - start >= CHAIN_INDEX_MAX_CATCHUP:
            # Отстали сильно — идём на голову, а не молотим историю: индекс это индекс, а не
            # архив. Пропуск честно объявляется дырой внутри _ingest.
            start = top
        for bn in range(start, top + 1):
            if self._stop.is_set():
                return
            try:
                self.scan_block(bn)
                if self._fail_streak:
                    self._log("CHAIN-INDEX: чтение восстановлено на блоке %d (после %d отказов)"
                              % (bn, self._fail_streak))
                self._fail_streak = 0
            except IndexBeyondHead as e:
                # Гонка потолка с подачей: блок ещё не запечатан. НЕ отказ, НЕ дыра, НЕ ERROR
                # и — главное — `_last_bn` не замораживается. Потолок объявляем протухшим.
                self.stats["beyond_head"] += 1
                self._head_conf_ts = 0.0
                # ...но «не отказ» не смеет стать ТИХИМ ЗАЛИПАНИЕМ. Если один и тот же номер
                # объявляется будущим десятки оборотов подряд, это уже не гонка с подачей, а
                # застрявший индекс — и молчать об этом нельзя (мёртвый сторож хуже отсутствующего).
                if bn == self._beyond_bn:
                    self._beyond_n += 1
                else:
                    self._beyond_bn, self._beyond_n = bn, 1
                if self._beyond_n >= CHAIN_INDEX_BEYOND_LOUD_N:
                    self._loud("CHAIN-INDEX: блок %d объявлен будущим %d раз подряд (узел=%s, "
                               "%s) — индекс СТОИТ на этом номере, покрытие не растёт"
                               % (bn, self._beyond_n, _host(self.url), str(e)[:80]), err=False)
                    self._beyond_n = 0
                return
            except Exception as e:
                self._note_fail(e, bn)
                return

    def _selfcheck(self):
        """Приёмочная строка первой минуты (пункт «а»): индекс сам говорит, читает ли он цепь.

        Ровно один раз. Молчание не имеет права быть неотличимым от «всё хорошо» — этим мы
        уже платили за мёртвых сторожей.
        """
        if self._selfchecked or (self._now() - self._t0) < CHAIN_INDEX_SELFCHECK_SEC:
            return
        self._selfchecked = True
        s = self.snapshot()
        why = []
        if s["blocks"] <= 0:
            why.append("НИ ОДНОГО блока не прочитано")
        if not s["fresh"]:
            why.append("индекс протух (%.1fс)" % s["age"])
        if s["gaps"] > 0:
            why.append("дыр=%d" % s["gaps"])
        msg = ("CHAIN-INDEX ПРИЁМКА(%.0fс): блоков=%d полных=%d лёгких=%d дыр=%d отказов=%d "
               "beyond=%d свежесть=%s голова=%s потолок=%d узел=%s (%s) — ВЕРДИКТ: %s"
               % (CHAIN_INDEX_SELFCHECK_SEC, s["blocks"], s["full"], s["light"], s["gaps"],
                  s["fails"], s["beyond_head"], s["fresh"], s["last"], s["conf"],
                  _host(self.url), CHAIN_INDEX_URL_SRC, "ЖИВ" if not why else "МЁРТВ — " + "; ".join(why)))
        if why:
            log.error("%s", msg)
        else:
            log.warning("%s", msg)

    def _note_fail(self, e, bn=None):
        self.stats["fails"] += 1
        self._fail_streak += 1
        self._loud("CHAIN-INDEX ОТКАЗ: узел=%s блок=%s (%s: %s) — подряд %d; индекс "
                   "ПРОТУХАЕТ, потребители обязаны видеть 'unknown', а не пустоту"
                   % (_host(self.url), bn, type(e).__name__, str(e)[:120], self._fail_streak))


# ---------------------------------------------------------------- синглтон
_INDEX = None
_INDEX_LOCK = threading.Lock()


def get_index():
    """Живой индекс либо None (выключен/не поднялся). None = потребитель идёт прежним путём."""
    return _INDEX


def start_index(cfg=None, watch_to=(), url=None, log_fn=None):
    """Поднять индекс. Возвращает экземпляр либо None.

    None бывает по двум причинам, и обе ГРОМКИЕ: механизм выключен ручкой отката (сообщаем на
    info) либо своего узла нет (сообщаем на error и НЕ подменяем его боевым: фон, залезший в
    боевой ключ, — это класс #100, за который мы уже платили).
    """
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is not None:
            return _INDEX
        if not CHAIN_INDEX_ON:
            log.info("CHAIN-INDEX выключен ручкой CHAIN_INDEX=0 — исходы проб читаются прежним "
                     "поштучным трактом квитанций (он лжёт под 429, см. #109)")
            return None
        u = url or CHAIN_INDEX_RPC_URL
        if not u:
            log.error("CHAIN-INDEX НЕ ПОДНЯТ: нет своего узла (CHAIN_INDEX_RPC_URL / BG_RPC_URL_2 "
                      "/ BG_RPC_URL пусты). Боевой ключ чтений для фона НЕ берём. Позиции наших "
                      "tx и tip транзита остаются неизмеримыми")
            return None
        # ГАРД БОЕВОГО КЛЮЧА. Фон, залезший в боевой ключ чтений, — это класс #100, за который
        # уже заплачено (наш собственный огонь топил боевой RPC). Проверяем НЕ намерение
        # («в списке боевого нет»), а РЕЗУЛЬТАТ: совпал ли выбранный адрес с боевым.
        _combat = _norm_url(getattr(cfg, "rpc_url", "") or "")
        if _combat and _norm_url(u) == _combat:
            log.error("CHAIN-INDEX НЕ ПОДНЯТ: выбранный узел (%s, источник %s) СОВПАЛ с боевым "
                      "ключом чтений. Фон на боевом ключе — это #100: наш же огонь топит RPC "
                      "ровно в окне победы. Задайте CHAIN_INDEX_RPC_URL отдельным адресом",
                      _host(u), CHAIN_INDEX_URL_SRC)
            return None
        if "ДЕЛИТ" in CHAIN_INDEX_URL_NOTE:
            log.error("CHAIN-INDEX: свободного bg-ключа нет — индекс ДЕЛИТ УЗЕЛ С КОЛЛЕКТОРОМ "
                      "квитанций (%s). Темпы складываются в одну квоту; замеренный расход "
                      "индекса печатается в hot-stats (rps/cu_day) — сверять его надо с "
                      "биллингом провайдера, а не со своим счётчиком", _host(u))
        watch = set(str(a).lower() for a in (watch_to or ()) if a)
        if getattr(cfg, "liquidator_address", ""):
            watch.add(str(cfg.liquidator_address).lower())    # пробы идут ИМЕННО туда (v5-доска)
        # Доп. адреса через запятую — на случай, если доска когда-нибудь переедет в отдельный
        # контракт: тогда позиции проб не пропадут из виду до следующего релиза.
        for a in os.environ.get("CHAIN_INDEX_WATCH", "").split(","):
            if a.strip():
                watch.add(a.strip().lower())
        idx = ChainIndex(u, watch_to=watch, log_fn=log_fn)
        idx.start()
        _INDEX = idx
        log.warning("CHAIN-INDEX узел: %s (источник %s, %s)",
                    _host(u), CHAIN_INDEX_URL_SRC, CHAIN_INDEX_URL_NOTE)
        if not watch:
            # Пустой список наблюдения = индекс видит цепь, но НЕ ВИДИТ НАС. Тихо это оставлять
            # нельзя: счётчики «наших tx=0» читались бы как «мы не стреляли» (класс «тест,
            # который может вернуть только ноль»).
            log.error("CHAIN-INDEX поднят БЕЗ НАБЛЮДАЕМЫХ КОНТРАКТОВ (LIQUIDATOR_ADDRESS пуст) "
                      "— позиции наших проб считаться НЕ будут, «наших tx=0» в статистике "
                      "означает слепоту, а не тишину")
        log.warning("CHAIN-INDEX поднят: узел=%s, наблюдаем %d контракт(ов), окно %d блоков "
                    "(откат CHAIN_INDEX=0 снимает механизм целиком)",
                    _host(u), len(watch), idx._keep)
        return idx


def offer_block(bn):
    """Подача блока из recv-петли. Дешевле некуда: индекса нет -> мгновенный выход."""
    i = _INDEX
    if i is not None:
        i.offer_block(bn)


def note_hash(h, head=None):
    i = _INDEX
    if i is not None:
        i.note_hash(h, head=head)


def stop_index():
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is not None:
            _INDEX.stop()
            _INDEX = None
