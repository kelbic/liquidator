"""12.08: КОРОТКИЙ КЛЮЧ ПРОВАЙДЕРА УХОДИЛ В ЖУРНАЛ ЦЕЛИКОМ.

Боевой факт: после рестарта в журнал легла строка
    [predict] основной узел отказал, ответил ЗАПАСНОЙ (429 ... for url: https://lb.drpc.live/base/<КЛЮЧ>)
Ключ dRPC в этой форме — 9 символов. Оба рубежа фильтра были слепы:
  * _SECRET_SEG_RE требует сегмент >=20 символов;
  * note_secret клал в реестр литералов только значения >=16.
А фильтр числился «закрытым навсегда» (#36) — то есть у нас была ложная уверенность.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.rpc as rpc                                        # noqa: E402

SHORT = "AjwysgVmw"                                            # форма боевого ключа: 9 симв.
URL = "https://lb.drpc.live/base/" + SHORT
LINE = "429 Client Error: Too Many Requests for url: " + URL

# --- 1. ПОЗИТИВНЫЙ КОНТРОЛЬ: до регистрации ключа строка НЕ чистится ---------------------
rpc._SECRETS.clear()
assert SHORT in rpc.redact(LINE), \
    "контроль недействителен: строка чистится ещё до регистрации ключа — тест ничего не докажет"
print("OK 1/5: контроль — незарегистрированный короткий ключ действительно виден (дефект живой)")

# --- 2. Регистрация нашего URL кладёт короткий ключ в реестр -----------------------------
rpc.note_secrets_from_url(URL)
assert SHORT in rpc._SECRETS, "короткий ключ не попал в реестр литералов — фикс не работает"
print("OK 2/5: ключ длиной %d принят в реестр (порог был 16)" % len(SHORT))

# --- 3. Строка чистится — И В URL, И «голая» (без схемы, как её печатает транспорт) ------
out = rpc.redact(LINE)
assert SHORT not in out, "ключ всё ещё в строке: %s" % out.replace(SHORT, "<НЕ ВЫРЕЗАН>")
assert "<ключ скрыт>" in out and "lb.drpc.live" in out, "потерян полезный текст: %s" % out
bare = rpc.redact("gaierror: lb.drpc.live/base/" + SHORT + " unreachable")
assert SHORT not in bare, "«голая» форма без схемы не чистится: %s" % bare
print("OK 3/5: ключ вырезан и в URL, и в строке без схемы; хост и текст ошибки сохранены")

# --- 4. Служебные сегменты НАШИХ же URL в реестр не попадают -----------------------------
rpc._SECRETS.clear()
for u in ("https://lb.drpc.live/base/" + SHORT,
          "https://base-mainnet.g.alchemy.com/v2/" + "K" * 32,
          "https://mainnet-preconf.base.org",
          "https://lb.drpc.live/ogrpc?network=base&dkey=" + "d" * 24):
    rpc.note_secrets_from_url(u)
for word in ("base", "v2", "ogrpc", "network", "mainnet", "rpc", ""):
    assert word not in rpc._SECRETS, "служебный сегмент %r принят за ключ — журнал будет изрешечён" % word
sample = "block 49887194: assessed=5394/5394 (full) liq=0 min_hf=1.0005 base mainnet v2"
assert rpc.redact(sample) == sample, "полезная строка испорчена редакцией: %s" % rpc.redact(sample)
print("OK 4/5: base/v2/ogrpc/network/mainnet за ключи НЕ приняты, обычная строка не тронута")

# --- 5. Длинные ключи по-прежнему режутся (регрессии старого поведения нет) --------------
long_key = "K" * 32
assert long_key in rpc._SECRETS
assert long_key not in rpc.redact("timeout for url: https://base-mainnet.g.alchemy.com/v2/" + long_key)
assert "d" * 24 not in rpc.redact("SSLError ... dkey=" + "d" * 24)
print("OK 5/5: длинные ключи и query-значения режутся как прежде")

print("=== короткий ключ провайдера больше не уходит в журнал ===")

# --- 6. РЕГИСТРАЦИЯ ИЗ ОКРУЖЕНИЯ: не полагаемся на «все контуры строят BaseRpc» -----------
# Утёкший URL предиктора до конструктора BaseRpc не доходил, поэтому в реестр не попадал
# вовсе. Комментарий рядом с note_secrets_from_url называл конструктор «ЕДИНСТВЕННОЙ дверью»
# — непроверенный инвариант, и он оказался ложным.
rpc._SECRETS.clear()
fake_env = {
    "PREDICT_RPC_URL": "https://lb.drpc.live/base/" + SHORT,
    "BG_RPC_URL_2": "https://lb.drpc.live/ogrpc?network=base&dkey=" + "z" * 24,
    "WALLET_KEY": "0x" + "9" * 64,          # не URL — трогать не должны
    "MODE": "live",
}
n = rpc.note_secrets_from_env(fake_env)
assert n == 2, "разобрано URL: %d (ожидалось 2 — только http(s))" % n
assert SHORT in rpc._SECRETS and "z" * 24 in rpc._SECRETS
assert ("0x" + "9" * 64) not in rpc._SECRETS, "приватник попал в реестр редакции — не его дело"
assert SHORT not in rpc.redact(LINE), "после регистрации из окружения ключ всё ещё виден"
print("OK 6/5: ключи подняты прямо из окружения, до всякого BaseRpc; не-URL не тронуты")

print("=== фильтр журнала больше не зависит от длины ключа и от пути его регистрации ===")

