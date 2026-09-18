"""Контроль гонки чтения раунда: журнальный a_new против ЛОГА AnswerUpdated в том же блоке.
Если бот прочитал latestRoundData до запечатывания блока, в журнале окажется ПРЕДЫДУЩИЙ раунд,
и настоящая цена B+6 будет посчитана по старому answer — то есть M1 занижается молча."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpcx
T_AU = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
FREE = ["https://base-rpc.publicnode.com", "https://base.drpc.org"]
D = os.path.dirname(os.path.abspath(__file__)) + "/data"
rows = [json.loads(l) for l in open(f"{D}/rounds.jsonl") if l.strip()]
print("раундов в журнале:", len(rows))
ok = bad = noev = 0
for r in rows[-25:]:
    lg = rpcx.getlogs(r["b"], r["b"], [T_AU], r["agg"], FREE)
    if not lg:
        noev += 1
        print("  b=%d agg=%s лога AnswerUpdated в блоке НЕТ (транзит без раунда)" % (r["b"], r["agg"][:10]))
        continue
    v = int(lg[-1]["topics"][1], 16)
    v = v - (1 << 256) if v >= (1 << 255) else v
    if v == r["a_new"]:
        ok += 1
    else:
        bad += 1
        print("  b=%d agg=%s ЖУРНАЛ a_new=%s != ЛОГ БЛОКА %s  <-- прочитан СТАРЫЙ раунд"
              % (r["b"], r["agg"][:10], r["a_new"], v))
print("ИТОГ: совпало %d, расхождений %d, транзитов без лога %d" % (ok, bad, noev))
