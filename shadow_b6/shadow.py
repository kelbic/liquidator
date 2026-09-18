"""ЖИВАЯ ТЕНЬ B+6 (Charlotte, 18.09-29.09.2026). НИ ОДНОЙ ОТПРАВЛЕННОЙ ТРАНЗАКЦИИ.

Что делает (протокол docs/research/SHADOW-B6-PROTOCOL.md):
  * читает поток флешблоков Base (IPv4, урок STATE 9934) и меряет ГРАНИЦЫ блоков —
    это знаменатель метрики M2 с n = все блоки, а не только призовые;
  * ловит транзит цены на отслеживаемые агрегаторы прямо во флешблоке, читает новый
    answer одним eth_call, считает цену, которую Morpho увидит в B+6, и точной
    математикой Morpho называет позиции, которые станут ликвидируемыми;
  * собирает и ПОДПИСЫВАЕТ транзакцию одноразовым ключом (нулевой баланс) — чтобы
    время t_ready было временем полной цепи, а не её огрызка, — и НЕ ОТПРАВЛЯЕТ;
  * на блоке отпирания читает факт: кто взял, в каком флешблоке, с каким tip, внутри
    аукциона Atlas или вне — и сравнивает с нашим моментом готовности (M4/M5).

Гарды: rpcx запрещает любой send-метод; приватный ключ бота не читается; из .env берутся
поимённо только read-URL.
"""
from __future__ import annotations
import asyncio, json, os, signal, socket, ssl, sys, threading, time, collections, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpcx, core

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = f"{HERE}/data"
FB_URL = "wss://mainnet.flashblocks.base.org/ws"
FB_HOST = "mainnet.flashblocks.base.org"
SEQ_HOST = "mainnet.base.org"        # тот же путь, которым уходила бы наша tx
T_AU = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
T_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
T_SVR = "0x8d530b9ddc4b318d28fdd4c3a21fcfecece54c1a72a824f262985b99afef009b"
SEL_TRANSMIT = ("6fadcf72", "b1dc65a4", "c9807539")   # transmit / transmitSecondary варианты

# --- политики, объявленные протоколом (§3.4). Менять по ходу прогона нельзя. ---
DELTA_MS = float(os.environ.get("SHADOW_DELTA_MS", "50"))
# Упреждение предсказательной политики: на сколько РАНЬШЕ ожидаемого локального прихода
# fb#0 блока B_rel надо отдать tx, чтобы она успела к сборке этого флешблока.
# День 1 — 150 мс; с 19.09 фиксируется как p10 замеренного зазора (§3.4 протокола).
PRED_LEAD_MS = float(os.environ.get("SHADOW_PRED_LEAD_MS", "150"))
TIP_SHARE = float(os.environ.get("SHADOW_TIP_SHARE", "0.35"))
TIP_CAP_GWEI = float(os.environ.get("SHADOW_TIP_CAP_GWEI", "400"))
TIP_FLOOR_GWEI = float(os.environ.get("SHADOW_TIP_FLOOR_GWEI", "0.05"))
GAS_EST = int(os.environ.get("SHADOW_GAS_EST", "900000"))     # медиана победителей 0.7-1.5M
ETH_USD = float(os.environ.get("SHADOW_ETH_USD", "4500"))
MIN_GROSS_USD = float(os.environ.get("SHADOW_MIN_GROSS_USD", "20"))
REL_OFFSET = int(os.environ.get("SHADOW_REL_OFFSET", "6"))    # B+6, проверяется §3.2
FULL_REFRESH_SEC = float(os.environ.get("SHADOW_FULL_REFRESH_SEC", "120"))
FULL_ALL_SEC = float(os.environ.get("SHADOW_FULL_ALL_SEC", "3600"))   # 92k позиций = 461 мультиколл на проход
UNIVERSE_RESCAN_SEC = float(os.environ.get("SHADOW_UNI_RESCAN_SEC", "21600"))

LOG = open(f"{DATA}/shadow.log", "a", buffering=1)
STOP = threading.Event()
LOCK = threading.Lock()


def log(*a):
    s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + " ".join(str(x) for x in a)
    LOG.write(s + "\n")


def jsonl(name, row):
    with open(f"{DATA}/{name}", "a", buffering=1) as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


class State:
    def __init__(self):
        self.markets = json.load(open(f"{HERE}/markets_tracked.json"))
        self.agg_of = collections.defaultdict(list)      # agg -> [(mid, leg_name)]
        for mid, m in self.markets.items():
            for name, leg in m["legs"].items():
                self.agg_of[leg["agg"]].append((mid, name))
        self.aggs = sorted(self.agg_of)
        self.universe = {}          # mid -> [borrowers]
        self.pos = {}               # mid -> {user: (coll, shares)}
        self.ctx = {}               # mid -> dict(tba,tbs,last_update,price,answers,block)
        self.fb = collections.OrderedDict()   # block -> {"first":t,"last":t,"idx":n,"tx":{hash:(idx,t)}}
        self.blk_ts = {}            # block -> timestamp
        self.head = 0
        self.rtt = 0.030            # до СЕКВЕНСЕРА (замер 16.08), фиксировано политикой
        self.edge_rtt = None        # TCP-connect до края base.org — контекст, не метрика
        self.c_offset = None        # t_recv(fb#0) - ts(block), локальная калибровка
        self.wm = time.time() - time.monotonic()   # мост wall -> monotonic (часы тикают синхронно)
        self.cnt = collections.Counter()
        self.pending = {}           # b_rel -> [decision rows]
        self.started = time.time()


ST = State()


# ---------------- вспомогательное ----------------
def urls_read():
    e = rpcx.env_urls("/root/liquidator/.env", ["ALCHEMY_C_URL", "BG_RPC_URL", "BLOCK_WSS_URL"])
    # порядок = приоритет. base.org отдаёт 429 на 50 из 64 запросов под нашим темпом
    # (замер 18.09), поэтому он последний; publicnode/drpc держат чисто.
    free = ["https://base-rpc.publicnode.com", "https://base.drpc.org", "https://mainnet.base.org"]
    paid = [v for k, v in e.items() if k == "ALCHEMY_C_URL" and v.startswith("http")]
    return free, paid


FREE, PAID = urls_read()


RTT_SEQ_MS = float(os.environ.get("SHADOW_RTT_MS", "30"))   # ЗАМЕР 16.08 до СЕКВЕНСЕРА


def measure_rtt():
    """TCP-connect до края Cloudflare перед base.org. ВНИМАНИЕ: это НЕ RTT до секвенсера.
    Память [[fleet-session-scope-and-premise-check]]: «RTT до секвенсера ПЕРЕМЕРЕН 16.08:
    30 мс, не 6» — ровно это число (6-7 мс) и было ошибкой. Поэтому в метриках стоит
    RTT_SEQ_MS (30 мс), а замер края пишется отдельным полем edge_rtt как контекст."""
    best = None
    for _ in range(3):
        try:
            t0 = time.monotonic()
            s = socket.create_connection((SEQ_HOST, 443), timeout=5)
            dt = time.monotonic() - t0
            s.close()
            best = dt if best is None else min(best, dt)
        except Exception:
            pass
        time.sleep(0.05)
    if best:
        ST.edge_rtt = best
    ST.rtt = RTT_SEQ_MS / 1000.0        # метрики считаем по замеру до СЕКВЕНСЕРА
    return best


def tip_gwei(gross_usd: float) -> float:
    want = TIP_SHARE * gross_usd / (GAS_EST * ETH_USD / 1e9)
    return max(TIP_FLOOR_GWEI, min(TIP_CAP_GWEI, want))


# ---------------- поток флешблоков ----------------
async def fb_loop():
    import websockets, brotli
    from eth_utils import keccak
    while not STOP.is_set():
        try:
            infos = socket.getaddrinfo(FB_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)
            fam, typ, proto, _c, sa = infos[0]
            sk = socket.socket(fam, typ, proto); sk.settimeout(20); sk.connect(sa); sk.settimeout(None)
            async with websockets.connect(FB_URL, sock=sk, ssl=ssl.create_default_context(),
                                          server_hostname=FB_HOST, open_timeout=20,
                                          ping_interval=20, max_size=None) as ws:
                log("fb: подключено")
                ST.cnt["fb_conn"] += 1
                while not STOP.is_set():
                    m = await asyncio.wait_for(ws.recv(), timeout=30)
                    t = time.monotonic(); tw = time.time()
                    if isinstance(m, (bytes, bytearray)):
                        try:
                            m = brotli.decompress(m)
                        except Exception:
                            pass
                    d = json.loads(m)
                    ST.cnt["fb_msg"] += 1
                    idx = d.get("index")
                    md = d.get("metadata") or {}
                    base = d.get("base") or {}
                    diff = d.get("diff") or {}
                    bn = md.get("block_number") or base.get("block_number")
                    if isinstance(bn, str):
                        bn = int(bn, 16) if bn.startswith("0x") else int(bn)
                    if not bn:
                        continue
                    txs = diff.get("transactions") or []
                    with LOCK:
                        e = ST.fb.get(bn)
                        if e is None:
                            e = {"first": t, "first_w": tw, "last": t, "idx": idx, "tx": {}, "n": 0}
                            ST.fb[bn] = e
                            while len(ST.fb) > 400:
                                ST.fb.popitem(last=False)
                            if bn > ST.head:
                                ST.head = bn
                                ST.cnt["blocks"] += 1
                        e["last"] = t; e["idx"] = idx; e["n"] += len(txs)
                        if base.get("timestamp"):
                            ts = base["timestamp"]
                            ST.blk_ts[bn] = int(ts, 16) if isinstance(ts, str) else int(ts)
                            if ST.c_offset is None:
                                ST.c_offset = tw - ST.blk_ts[bn]
                            else:
                                ST.c_offset = 0.98 * ST.c_offset + 0.02 * (tw - ST.blk_ts[bn])
                    for raw in txs:
                        try:
                            h = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
                        except Exception:
                            continue
                        with LOCK:
                            e["tx"][h] = (idx, t)
                        low = raw.lower()
                        for agg in ST.aggs:
                            if agg[2:] in low:
                                on_transit(agg, bn, t, tw, h)
                                break
        except Exception as ex:
            log("fb: обрыв", type(ex).__name__, str(ex)[:120])
            ST.cnt["fb_err"] += 1
            await asyncio.sleep(2.0)


# ---------------- обнаружение раунда и решение ----------------
_last_round = {}


def on_transit(agg: str, block: int, t_mono: float, t_wall: float, txh: str):
    """Транзит замечен во флешблоке. Тяжёлую часть уводим в поток, чтобы не душить recv-цикл
    (урок STATE: медленный потребитель = сервер рвёт WS)."""
    if _last_round.get(agg) == block:
        return
    _last_round[agg] = block
    threading.Thread(target=_decide, args=(agg, block, t_mono, t_wall, txh), daemon=True).start()


def _read_round(agg, block, prev):
    """Новый answer раунда с АГРЕГАТОРА. ГОНКА, пойманная контролем 18.09: `latest` у RPC —
    это последний ЗАПЕЧАТАННЫЙ блок, а транзит мы видим во ФЛЕШБЛОКЕ, за ~2 с до запечатывания.
    Чтение сразу после детекта давало ПРЕДЫДУЩИЙ раунд — 10 из 10 проверенных раундов
    (check_round_race.py). Молчаливое занижение M1: цена B+6 считалась бы по старому answer.
    Ждём, пока поток флешблоков не покажет следующий блок (значит `block` запечатан), и только
    потом читаем; ожидание бесплатно — до B+6 у нас 12 с."""
    deadline = time.monotonic() + 5.0
    while ST.head <= block and time.monotonic() < deadline:
        time.sleep(0.05)
    # Читаем ЛОГ AnswerUpdated ИМЕННО ЭТОГО БЛОКА, а не latestRoundData: условие
    # «roundId больше прошлого» пропускало БОЛЕЕ НОВЫЙ раунд (следующего блока) и привязывало
    # его к нашему B — 2 расхождения из 13 после первого фикса. Лог однозначно связывает
    # answer с блоком, и цена B+6 считается по тому раунду, который в этом блоке и вышел.
    for attempt in range(6):
        try:
            lg = rpcx.getlogs(block, block, [T_AU], agg, FREE + PAID, timeout=10)
        except Exception:
            lg = []
        if lg:
            t = lg[-1]["topics"]
            v = int(t[1], 16)
            a_new = v - (1 << 256) if v >= (1 << 255) else v
            round_id = int(t[2], 16)
            updated_at = int((lg[-1]["data"] or "0x0")[2:66] or "0", 16)
            return round_id, a_new, updated_at, attempt
        if time.monotonic() > deadline + 3.0:
            break
        time.sleep(0.25)
    return None, None, None, -1


def _decide(agg, block, t_mono, t_wall, txh):
    try:
        ST.cnt["transit"] += 1
        prev = _last_round.get("ans:" + agg)
        round_id, a_new, updated_at, tries = _read_round(agg, block, prev)
        t_answer = time.monotonic()
        if round_id is None:
            ST.cnt["transit_noround"] += 1
            return
        _last_round["ans:" + agg] = round_id
        ST.cnt["rounds"] += 1
        b_rel = block + REL_OFFSET
        decided = []
        for mid, leg in ST.agg_of[agg]:
            m = ST.markets[mid]
            ctx = ST.ctx.get(mid)
            pos = ST.pos.get(mid)
            if not ctx or not pos:
                continue
            a_old = (ctx["answers"] or {}).get(leg)
            if not a_old:
                continue
            price_new = ctx["price"] * a_new // a_old
            mk = {"id": mid, "lltv": m["lltv"], "tba": ctx["tba"], "tbs": ctx["tbs"],
                  "rate": 0, "last_update": ctx["last_update"],
                  "ts": ST.blk_ts.get(block, 0) + 2 * REL_OFFSET}
            dec = int(m["loan_dec"])
            named = core.detect(mk, ((u, c, sh) for u, (c, sh) in pos.items()), price_new,
                                min_gross_units=int(MIN_GROSS_USD * 10 ** dec))
            if named:
                decided.append((mid, m, named, price_new, dec))
        if not decided:
            jsonl("rounds.jsonl", {"t": t_wall, "agg": agg, "b": block, "b_rel": b_rel,
                                   "round": round_id, "a_new": a_new, "named": 0,
                                   "lat_ms": round((time.monotonic() - t_mono) * 1000, 1),
                                   "answer_wait_ms": round((t_answer - t_mono) * 1000, 1),
                                   "tries": tries})
            return
        # 2) сборка и ПОДПИСЬ (одноразовый ключ, нулевой баланс) — чтобы t_ready был честным
        shots = []
        for mid, m, named, price_new, dec in decided:
            for r in named[:8]:
                gross_usd = r["gross"] / 10 ** dec
                tg = tip_gwei(gross_usd)
                raw = _build_and_sign(mid, r, tg)
                shots.append({"mid": mid, "pair": m["pair"], "user": r["user"],
                              "repay_usd": r["repay"] / 10 ** dec, "gross_usd": gross_usd,
                              "hf": round(r["hf"], 6), "tip_gwei": round(tg, 4),
                              "raw_len": len(raw)})
        t_ready = time.monotonic()
        row = {"t": t_wall, "agg": agg, "b": block, "b_rel": b_rel, "round": round_id,
               "a_new": a_new, "named": len(shots), "shots": shots,
               "lat_ms": round((t_ready - t_mono) * 1000, 1),
               "answer_wait_ms": round((t_answer - t_mono) * 1000, 1),
               "compute_ms": round((t_ready - t_answer) * 1000, 1), "tries": tries,
               "t_ready_mono": t_ready, "rtt": round(ST.rtt, 4),
               "c_offset": ST.c_offset, "txh_transit": txh}
        jsonl("rounds.jsonl", row)
        with LOCK:
            ST.pending.setdefault(b_rel, []).append(row)
        ST.cnt["decisions"] += 1
        log(f"РАУНД {agg[:10]} b={block} -> b_rel={b_rel} назвал {len(shots)} "
            f"(top ${max(x['gross_usd'] for x in shots):.0f}) за {row['lat_ms']}мс")
    except Exception as ex:
        ST.cnt["decide_err"] += 1
        log("decide: ошибка", type(ex).__name__, str(ex)[:160])


_KEY = None


def _build_and_sign(mid, r, tip_g):
    """Собирает и подписывает liquidate() ОДНОРАЗОВЫМ ключом. Никакой отправки: нет вызова
    send* ни здесь, ни в rpcx (гард). Цель — честное время полной цепи."""
    global _KEY
    from eth_account import Account
    from eth_abi import encode
    if _KEY is None:
        _KEY = Account.create()          # только в памяти, на диск не пишется
    m = ST.markets[mid]
    data = rpcx.sel("liquidate((address,address,address,address,uint256),address,uint256,uint256,bytes)") + encode(
        ["(address,address,address,address,uint256)", "address", "uint256", "uint256", "bytes"],
        [(m["loan"], m["coll"], m["oracle"], "0x46415998764C29aB2a25CbeA6254146D50D22687", m["lltv"]),
         r["user"], int(r["seized"]), 0, b""])
    tx = {"chainId": 8453, "nonce": 0, "to": rpcx.MORPHO, "value": 0, "gas": GAS_EST,
          "maxFeePerGas": int((tip_g + 0.2) * 1e9), "maxPriorityFeePerGas": int(tip_g * 1e9),
          "data": data}
    signed = Account.sign_transaction(tx, _KEY.key)
    return signed.raw_transaction


# ---------------- факт на блоке отпирания ----------------
def outcome_loop():
    while not STOP.is_set():
        time.sleep(1.0)
        with LOCK:
            due = [b for b in list(ST.pending) if ST.head >= b + 2]
        for b_rel in due:
            with LOCK:
                rows = ST.pending.pop(b_rel, [])
            try:
                _outcome(b_rel, rows)
            except Exception as ex:
                ST.cnt["outcome_err"] += 1
                log("outcome: ошибка", b_rel, type(ex).__name__, str(ex)[:140])


def _outcome(b_rel, rows):
    # Отпирание Atlas между раундом и B+6: цена выходит раньше, ликвидация идёт в блоке
    # победителя аукциона, и наш выстрел реальный бот отменил бы. Без этой проверки раунд
    # читается как «назвали N, событий 0» — то есть как ПРОИГРАННАЯ гонка.
    b_round = min((r["b"] for r in rows), default=b_rel - REL_OFFSET)
    unlocked = []
    try:
        ul = rpcx.getlogs(b_round, b_rel - 1, [T_SVR], None, FREE + PAID, timeout=20)
        aggs_round = {r["agg"] for r in rows}
        unlocked = sorted({l["address"].lower() for l in ul if l["address"].lower() in aggs_round})
    except Exception as ex:
        log("outcome: отпирания не прочитаны", str(ex)[:80])
    if unlocked:
        ST.cnt["rounds_atlas_unlocked"] += 1
        for r in rows:
            jsonl("atlas_unlocked.jsonl", {"b_rel": b_rel, "b": r["b"], "agg": r["agg"],
                                           "named": r.get("named"), "unlocked": unlocked})
        log(f"ОТПИРАНИЕ ATLAS до B+6 на {b_rel}: {unlocked} — выстрелы отменяются (не проигрыш)")
    logs = rpcx.getlogs(b_rel, b_rel, [T_LIQ], rpcx.MORPHO, FREE + PAID, timeout=20)
    hdr = rpcx.rpc("eth_getBlockByNumber", [hex(b_rel), False], FREE + PAID, timeout=20)
    base_fee = int(hdr["baseFeePerGas"], 16)
    svr_logs = rpcx.getlogs(b_rel, b_rel, [T_SVR], None, FREE + PAID, timeout=20)
    svr_tx = {l["transactionHash"].lower() for l in svr_logs}
    svr_first_txi = min([int(l["transactionIndex"], 16) for l in svr_logs], default=None)
    named = {}
    for r in rows:
        for s in r["shots"]:
            named[(s["mid"], s["user"].lower())] = (r, s)
    ev = []
    rcpts = {}
    if logs:
        hs = sorted({l["transactionHash"] for l in logs})
        res = rpcx.batch([("eth_getTransactionReceipt", [h]) for h in hs], FREE + PAID, timeout=40)
        for h, r in zip(hs, res):
            if not isinstance(r, Exception) and r:
                rcpts[h.lower()] = r
    for l in logs:
        txh = l["transactionHash"].lower()
        mid = l["topics"][1].lower()
        user = ("0x" + l["topics"][3][-40:]).lower()
        dat = bytes.fromhex(l["data"][2:])
        repaid = int.from_bytes(dat[0:32], "big")
        rc = rcpts.get(txh) or {}
        txi = int(rc.get("transactionIndex", "0x0"), 16)
        egp = int(rc.get("effectiveGasPrice", "0x0"), 16)
        cls = "A_UNLOCK" if txh in svr_tx else ("B_ADJ" if (svr_first_txi is not None and svr_first_txi < txi) else "C_OUTSIDE")
        with LOCK:
            fbe = ST.fb.get(b_rel) or {}
            fbinfo = (fbe.get("tx") or {}).get(txh)
            prev = ST.fb.get(b_rel - 1) or {}
            t_fb0 = fbe.get("first")
            t_prev_last = prev.get("last")
            t_last = fbe.get("last")
        row = {"b_rel": b_rel, "atlas_unlocked": unlocked, "mid": mid,
               "pair": (ST.markets.get(mid) or {}).get("pair"),
               "user": user, "repaid": repaid, "tx": txh, "txi": txi, "cls": cls,
               "tip_gwei": round((egp - base_fee) / 1e9, 6), "base_gwei": round(base_fee / 1e9, 6),
               "gas": int(rc.get("gasUsed", "0x0"), 16),
               "winner_fb_idx": (fbinfo[0] if fbinfo else None),
               "winner_fb_t": (fbinfo[1] if fbinfo else None),
               "fb0_t": t_fb0, "fb_last_t": t_last, "prev_last_t": t_prev_last}
        key = (mid, user)
        r_s = named.get(key)
        if r_s:
            r, s = r_s
            t_send = None
            ts_rel = ST.blk_ts.get(b_rel)
            if ts_rel is not None and ST.c_offset is not None:
                t_send = (ts_rel + ST.c_offset) - ST.wm - PRED_LEAD_MS / 1000.0   # предсказательная
            elif t_prev_last is not None:
                t_send = t_prev_last + DELTA_MS / 1000.0              # хиндсайт-запас     # политика §3.4
            arrive = (t_send + ST.rtt / 2) if t_send is not None else None
            row["ours"] = {"lat_ms": r["lat_ms"], "t_ready_mono": r["t_ready_mono"],
                           "tip_policy_gwei": s["tip_gwei"], "gross_usd": s["gross_usd"],
                           "repay_usd_pred": s["repay_usd"],
                           "m4_in_block": bool(t_last is not None and r["t_ready_mono"] + ST.rtt <= t_last),
                           "arrive_mono": arrive}
            if arrive is not None and fbinfo:
                seal_win = fbinfo[1] - ST.rtt / 2
                same_or_earlier = arrive <= seal_win
                tip_wins = s["tip_gwei"] > row["tip_gwei"]
                row["ours"]["m5_strict"] = bool(same_or_earlier and (fbinfo[0] != 0 or tip_wins))
                row["ours"]["m5_time_ok"] = bool(same_or_earlier)
                row["ours"]["m5_tip_ok"] = bool(tip_wins)
        ev.append(row)
        jsonl("outcomes.jsonl", row)
    # §3.2 протокола: блок отпирания ПРОВЕРЯТЬ, а не предполагать. Выборка 1 раунд из 10:
    # читаем price() оракула на B_rel-1 и B_rel и записываем ФАКТИЧЕСКОЕ смещение.
    try:
        if rows and (b_rel % 10 == 0):
            s_ = rpcx.selectors()
            mids = sorted({m for r in rows for m, _l in ST.agg_of.get(r["agg"], [])})[:2]
            for mid in mids:
                orc = ST.markets[mid]["oracle"]
                p_prev = rpcx.multicall([(orc, "0x" + s_["price"].hex())], FREE, block=b_rel - 1)[0]
                p_now = rpcx.multicall([(orc, "0x" + s_["price"].hex())], FREE, block=b_rel)[0]
                if p_prev[0] and p_now[0]:
                    v0, v1 = rpcx.u256(p_prev[1]), rpcx.u256(p_now[1])
                    jsonl("release_check.jsonl", {"b_rel": b_rel, "mid": mid, "price_B5": v0,
                                                  "price_B6": v1, "changed": v0 != v1,
                                                  "b_round": b_round, "offset": b_rel - b_round})
                    ST.cnt["rel_check"] += 1
                    ST.cnt["rel_changed"] += 1 if v0 != v1 else 0
    except Exception as ex:
        log("release-check: ошибка", str(ex)[:90])
    ST.cnt["events"] += len(ev)
    ST.cnt["events_outside"] += sum(1 for r in ev if r["cls"] == "C_OUTSIDE")
    ST.cnt["events_named"] += sum(1 for r in ev if r.get("ours"))
    if ev:
        log(f"ИСХОД b_rel={b_rel}: событий {len(ev)} "
            f"(вне аукциона {sum(1 for r in ev if r['cls']=='C_OUTSIDE')}), "
            f"наших названных {sum(1 for r in ev if r.get('ours'))}")


# ---------------- измерение границ блоков (M2, n = все блоки) ----------------
def boundary_loop():
    seen = set()
    while not STOP.is_set():
        time.sleep(2.0)
        with LOCK:
            blocks = sorted(ST.fb)
            c = ST.c_offset
            rtt = ST.rtt
        for bn in blocks[:-2]:
            if bn in seen:
                continue
            with LOCK:
                e = ST.fb.get(bn); p = ST.fb.get(bn - 1); ts = ST.blk_ts.get(bn)
            if not e or not p or e.get("first") is None or p.get("last") is None:
                continue
            seen.add(bn)
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])
            # окно попадания в флешблок 0 блока bn, в ЛОКАЛЬНЫХ часах:
            #   t_recv(last fb of bn-1) - RTT  <  t_send  <  t_recv(fb0 of bn) - RTT
            lo = p["last"] - rtt
            hi = e["first"] - rtt
            # (а) ХИНДСАЙТ-политика: «последний флешблок предыдущего блока» известен только
            #     ПОСЛЕ прихода fb0 -> это верхняя граница способности, не способность.
            t_hind = p["last"] + DELTA_MS / 1000.0
            ok_h = lo < t_hind <= hi
            # (б) ПРЕДСКАЗАТЕЛЬНАЯ политика (то, что бот может сделать в реальном времени):
            #     границу блока берём из ЧАСОВ ЦЕПИ ts(bn) + калибровка c_offset.
            ok_p = None
            t_pred = None
            lo_rel = hi_rel = None
            if ts is not None and c is not None:
                # ts+c — ожидаемое ВРЕМЯ СТЕНЫ прихода fb0 блока bn; переводим в монотонное
                # (иначе сравниваются секунды эпохи с секундами аптайма — дефект первого прогона).
                exp_fb0 = (ts + c) - ST.wm
                t_pred = exp_fb0 - PRED_LEAD_MS / 1000.0
                ok_p = lo < t_pred <= hi
                # ГЕОМЕТРИЯ ОКНА относительно ожидаемого прихода fb0 — чтобы ЛЮБОЕ упреждение
                # можно было оценить офлайн по этим же данным, не переигрывая прогон.
                lo_rel = round((lo - exp_fb0) * 1000, 1)
                hi_rel = round((hi - exp_fb0) * 1000, 1)
            jsonl("boundaries.jsonl", {"b": bn, "gap_ms": round((e["first"] - p["last"]) * 1000, 1),
                                       "fb_span_ms": round((e["last"] - e["first"]) * 1000, 1),
                                       "idx_last_prev": p.get("idx"), "ntx": e.get("n"),
                                       "in_fb0": bool(ok_h), "in_fb0_pred": ok_p,
                                       "win_ms": round((hi - lo) * 1000, 1),
                                       "lo_rel_ms": lo_rel, "hi_rel_ms": hi_rel,
                                       "lead_ms": PRED_LEAD_MS,
                                       "rtt_ms": round(rtt * 1000, 1)})
            ST.cnt["bnd"] += 1
            ST.cnt["bnd_ok"] += 1 if ok_h else 0
            if ok_p is not None:
                ST.cnt["bnd_pred"] += 1
                ST.cnt["bnd_pred_ok"] += 1 if ok_p else 0


# ---------------- состояние: позиции и контексты ----------------
def refresh_ctx():
    s = rpcx.selectors()
    calls = []
    keys = []
    for mid, m in ST.markets.items():
        calls.append((rpcx.MORPHO, rpcx.enc_bytes32(s["market"], mid))); keys.append((mid, "market"))
        calls.append((m["oracle"], "0x" + s["price"].hex())); keys.append((mid, "price"))
        for name, leg in sorted(m["legs"].items()):
            calls.append((leg["feed"], "0x" + s["latestRoundData"].hex())); keys.append((mid, "leg:" + name))
    rows = rpcx.multicall(calls, rotate(FREE) + PAID, block="latest", chunk=200, timeout=40)
    acc = {}
    for (mid, what), (ok, d) in zip(keys, rows):
        if not ok:
            continue
        e = acc.setdefault(mid, {"answers": {}})
        if what == "market" and len(d) >= 192:
            e.update({"tba": rpcx.u256(d, 2), "tbs": rpcx.u256(d, 3), "last_update": rpcx.u256(d, 4)})
        elif what == "price":
            e["price"] = rpcx.u256(d)
        elif what.startswith("leg:") and len(d) >= 64:
            e["answers"][what[4:]] = rpcx.i256(d, 1)
    with LOCK:
        for mid, e in acc.items():
            if "tba" in e and "price" in e:
                ST.ctx[mid] = e
    ST.cnt["ctx_refresh"] += 1


_rot = [0]


def rotate(urls):
    """Ротация только по «чистым» эндпоинтам; base.org остаётся последним фолбэком."""
    """Ротация свободных эндпоинтов: 65 мультиколлов подряд в один адрес давали 40-80 %
    отказов по рейту (замер 18.09 на старте) — фолбэк их съедал, но расход удваивался."""
    _rot[0] += 1
    head, tail = urls[:2], urls[2:]
    k = _rot[0] % max(1, len(head))
    return head[k:] + head[:k] + tail


HOT_HF = float(os.environ.get("SHADOW_HOT_HF", "1.10"))
HOT_CAP = int(os.environ.get("SHADOW_HOT_CAP", "2000"))


def hot_users(mid, hf_max=None):
    """Позиции у кромки — считаются ЛОКАЛЬНО из кэша на текущей цене, бесплатно."""
    hf_max = HOT_HF if hf_max is None else hf_max
    ctx = ST.ctx.get(mid); pos = ST.pos.get(mid) or {}
    m = ST.markets.get(mid)
    if not ctx or not m or "price" not in ctx:
        # без контекста «горячим» становится ВЕСЬ рынок — это и раздувало расход (замер 18.09:
        # 0.64 запроса/с). Ограничиваем капом, а не молча читаем всё.
        return sorted(pos)[:HOT_CAP]
    scored = []
    for u, (co, sh) in pos.items():
        _b, _mb, _liq, hf = core.health(co, sh, ctx["price"], m["lltv"], ctx["tba"], ctx["tbs"])
        if hf <= hf_max:
            scored.append((hf, u))
    scored.sort()
    return [u for _hf, u in scored[:HOT_CAP]]


def refresh_positions(full=False):
    s = rpcx.selectors()
    n = 0
    for mid, borrowers in ST.universe.items():
        if not borrowers:
            continue
        cur = ST.pos.get(mid) or {}
        target = borrowers if (full or not cur) else hot_users(mid)
        calls = [(rpcx.MORPHO, rpcx.enc_position(mid, b)) for b in target]
        if not calls:
            continue
        try:
            rows = rpcx.multicall(calls, rotate(FREE), block="latest", chunk=200, timeout=60)
        except Exception as ex:
            log("positions: ошибка", mid[:10], str(ex)[:100]); continue
        new = {}
        for b, (ok, d) in zip(target, rows):
            if ok and len(d) >= 96:
                sh = rpcx.u256(d, 1); co = rpcx.u256(d, 2)
                if sh > 0 and co > 0:
                    new[b] = (co, sh)
        with LOCK:
            if full or not cur:
                ST.pos[mid] = new
            else:
                for k in target:                     # только про выбранных знаем актуальное
                    if k in new:
                        cur[k] = new[k]
                    else:
                        cur.pop(k, None)
                ST.pos[mid] = cur
        n += len(ST.pos.get(mid) or {})
    ST.cnt["pos_refresh"] += 1
    ST.cnt["pos_live"] = n
    return n


_T_BORROW = None
_T_SUPCOLL = None
_uni_scanned = [0]


def grow_universe():
    """Инкрементальный добор вселенной: заёмщик, взявший долг В ХОДЕ прогона, обязан попасть
    в список — иначе тень слепнет ровно на свежих позициях (в реплее это дало 11 промахов из 81,
    все — «пропуск вселенной»). Один getLogs на цикл."""
    global _T_BORROW, _T_SUPCOLL
    if _T_BORROW is None:
        _T_BORROW = "0x" + rpcx.keccak("Borrow(bytes32,address,address,address,uint256,uint256)").hex()
        _T_SUPCOLL = "0x" + rpcx.keccak("SupplyCollateral(bytes32,address,address,uint256)").hex()
    head = ST.head
    if not head:
        return 0
    frm = _uni_scanned[0] or (head - 30)
    if head - frm > 1900:
        frm = head - 1900
    if head <= frm:
        return 0
    ids = sorted(ST.universe) or sorted(ST.markets)
    logs = rpcx.getlogs(frm, head, [[_T_BORROW, _T_SUPCOLL], [i for i in ids]], rpcx.MORPHO, FREE, timeout=25)
    added = 0
    for l in logs:
        t = l["topics"]
        mid = t[1].lower()
        if mid not in ST.universe:
            continue
        who = ("0x" + t[2][-40:]).lower() if t[0].lower() == _T_BORROW else ("0x" + t[3][-40:]).lower()
        if who not in ST.universe[mid]:
            ST.universe[mid].append(who)
            added += 1
    _uni_scanned[0] = head + 1
    if added:
        ST.cnt["uni_added"] += added
        log(f"вселенная: +{added} заёмщиков (блоки {frm}..{head})")
    return added


def state_loop():
    ST.universe = json.load(open(f"{HERE}/data/universe_live.json"))
    log(f"вселенная: {sum(len(v) for v in ST.universe.values())} позиций на {len(ST.universe)} рынках")
    refresh_ctx()
    n = refresh_positions(full=True)
    log(f"позиций с долгом: {n}")
    last_full = time.time()
    while not STOP.is_set():
        t0 = time.time()
        try:
            refresh_ctx()
            try:
                added = grow_universe()
            except Exception as ex:
                added = 0
                log("uni: ошибка", str(ex)[:90])
            full = (time.time() - last_full) > FULL_ALL_SEC or added > 0
            if full:
                # вселенную перечитываем с диска: догон истории (merge_universe.py) должен
                # подхватываться без рестарта, иначе «расширили вселенную» останется на бумаге
                try:
                    fresh = json.load(open(f"{HERE}/data/universe_live.json"))
                    add = sum(len(set(v) - set(ST.universe.get(k) or [])) for k, v in fresh.items())
                    if add:
                        for k, v in fresh.items():
                            ST.universe[k] = sorted(set(ST.universe.get(k) or []) | set(v))
                        log(f"вселенная перечитана с диска: +{add} заёмщиков, всего "
                            f"{sum(len(v) for v in ST.universe.values())}")
                except Exception as ex:
                    log("uni-reload: ошибка", str(ex)[:80])
            refresh_positions(full=full)
            if full:
                last_full = time.time()
        except Exception as ex:
            log("state: ошибка", type(ex).__name__, str(ex)[:120])
        STOP.wait(max(5.0, FULL_REFRESH_SEC - (time.time() - t0)))


def hb_loop():
    pidfile = f"{DATA}/shadow.pid"
    open(pidfile, "w").write(str(os.getpid()))
    while not STOP.is_set():
        try:
            hb = {"wall": time.time(), "mono": time.monotonic(), "pid": os.getpid(),
                  "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "up_sec": round(time.time() - ST.started, 1), "head": ST.head,
                  "rtt_ms": round(ST.rtt * 1000, 1),
                  "edge_rtt_ms": round((ST.edge_rtt or 0) * 1000, 1), "c_offset": ST.c_offset,
                  "cnt": dict(ST.cnt), "rpc": rpcx.stats(),
                  "pos_markets": len(ST.pos), "pending": len(ST.pending)}
            tmp = f"{DATA}/heartbeat.json.tmp"
            json.dump(hb, open(tmp, "w"))
            os.replace(tmp, f"{DATA}/heartbeat.json")
        except Exception as ex:
            log("hb: ошибка", str(ex)[:100])
        STOP.wait(10.0)


def rtt_loop():
    while not STOP.is_set():
        measure_rtt()
        STOP.wait(300.0)


_LOCK_FH = None


def single_instance():
    """Единственный экземпляр — через flock, а не через pidfile: сторож и рука оператора
    могут стартовать одновременно, и два потока решений писали бы в одни файлы."""
    global _LOCK_FH
    import fcntl
    _LOCK_FH = open(f"{DATA}/shadow.lock", "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("СТАРТ ОТМЕНЁН: экземпляр тени уже держит data/shadow.lock")
        print("already running", file=sys.stderr)
        return False
    _LOCK_FH.write(str(os.getpid())); _LOCK_FH.flush()
    return True


def main():
    if not single_instance():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_a: STOP.set())
    log("=" * 70)
    log("СТАРТ тени B+6. Отправка транзакций невозможна (гард rpcx). pid", os.getpid())
    measure_rtt()
    log(f"RTT метрик (секвенсер, замер 16.08): {ST.rtt*1000:.1f} мс; "
        f"TCP-край {SEQ_HOST}: {(ST.edge_rtt or 0)*1000:.1f} мс (в метриках НЕ используется)")
    threading.Thread(target=hb_loop, daemon=True).start()
    threading.Thread(target=rtt_loop, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    threading.Thread(target=outcome_loop, daemon=True).start()
    threading.Thread(target=boundary_loop, daemon=True).start()
    try:
        asyncio.run(fb_loop())
    except KeyboardInterrupt:
        pass
    STOP.set()
    log("СТОП тени")


if __name__ == "__main__":
    main()
