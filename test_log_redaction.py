"""Редакция ключей провайдеров в журнале.

Повод (07.08): в journal открытым текстом уходил полный `https://lb.drpc.live/base/<ключ>` —
~600 строк в сутки из «[predict] aggregator poll failed». Текст исключения RPC несёт URL
целиком, а его печатали как есть. Ключ в пути URL — секрет ровно того же веса, что и приватник.

Ключи здесь ТОЛЬКО синтетические: боевое значение не должно попасть в репозиторий даже как
фикстура (см. память secrets-never-grep-env).
"""
import logging
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from chain.rpc import redact, RedactingFilter          # noqa: E402

FAKE_DRPC = "AaaaaaaaaaBbbbbbbbbbCcccccccccDdddddddddEee"   # 43 симв., как у drpc
FAKE_ALCH = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"             # 32 симв., как у Alchemy

# --- 1. ключ вырезан, хост остался -------------------------------------------------------
u = "https://lb.drpc.live/base/" + FAKE_DRPC
r = redact("429 Client Error: Too Many Requests for url: " + u)
assert FAKE_DRPC not in r, "ключ drpc остался в строке: %s" % r
assert "lb.drpc.live" in r, "хост потерян — по логу не понять, какой контур отвалился: %s" % r
assert "<ключ скрыт>" in r, r

u2 = "https://base-mainnet.g.alchemy.com/v2/" + FAKE_ALCH
r2 = redact("health-fail на " + u2)
assert FAKE_ALCH not in r2, r2
assert "base-mainnet.g.alchemy.com" in r2, r2

# --- 2. то, что резать НЕЛЬЗЯ ------------------------------------------------------------
# Хэш tx и адрес — единственный материал вскрытия гонки. Пере-редакция ломает разбор так же
# верно, как недо-редакция ломает секретность.
tx = "0x6cf59693" + "a" * 56
addr = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
keep = redact("VOLLEY проба %s -> %s ok" % (tx, addr))
assert tx in keep, "хэш tx вырезан: %s" % keep
assert addr in keep, "адрес вырезан: %s" % keep

# URL без ключа обязан остаться читаемым целиком
assert redact("https://mainnet.base.org") == "https://mainnet.base.org"
assert redact("https://1rpc.io/base") == "https://1rpc.io/base"

# --- 3. ключ в query, а не в пути --------------------------------------------------------
r3 = redact("https://example-rpc.io/rpc?apikey=" + FAKE_ALCH)
assert FAKE_ALCH not in r3, r3

# --- 4. фильтр на хендлере: и msg, и args, и traceback -----------------------------------
class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


cap = _Cap()
cap.setFormatter(logging.Formatter("%(message)s"))
cap.addFilter(RedactingFilter())
lg = logging.getLogger("liq.test_redaction")
lg.setLevel(logging.INFO)
lg.propagate = False
lg.handlers = [cap]

# готовая строка (так пишет predict.py — % применён до вызова лога)
lg.info("[predict] aggregator poll failed (skipped): 429 for url: " + u)
# ленивое форматирование через args
lg.info("COMBAT-FAILOVER health-fail на %s", u2)
# traceback: log.exception рендерит exc_info отдельно от msg
try:
    raise RuntimeError("connect failed: " + u)
except RuntimeError:
    lg.exception("BOARD setPlan send fail")

blob = "\n".join(cap.lines)
assert FAKE_DRPC not in blob, "ключ прошёл сквозь фильтр:\n%s" % blob
assert FAKE_ALCH not in blob, "ключ прошёл сквозь фильтр:\n%s" % blob
assert "lb.drpc.live" in blob and "alchemy.com" in blob, blob
assert "RuntimeError" in blob, "traceback потерян — фильтр не должен глушить диагноз:\n%s" % blob

# --- 5. КОНТРОЛЬ: без фильтра ключ действительно утекал бы -------------------------------
# Иначе «ключа в выводе нет» неотличимо от «строку вообще не писали».
bare = _Cap()
bare.setFormatter(logging.Formatter("%(message)s"))
lg.handlers = [bare]
lg.info("[predict] aggregator poll failed (skipped): 429 for url: " + u)
assert FAKE_DRPC in "\n".join(bare.lines), "контроль не сработал: строка не дошла до хендлера"

# --- 6. _diag: дефект кода назван дефектом кода ------------------------------------------
from chain.spec_fire_live import _diag                  # noqa: E402

d = _diag(NameError("name '_fresh_basefee' is not defined"))
assert "NameError" in d and "ДЕФЕКТ КОДА" in d and "_fresh_basefee" in d, d
d2 = _diag(RuntimeError("timeout to " + u))
assert "ДЕФЕКТ КОДА" not in d2, d2
assert FAKE_DRPC not in d2, "_diag протащил ключ: %s" % d2

print("OK: ключи режутся, хост/хэш/адрес/traceback остаются, контроль сошёлся")
