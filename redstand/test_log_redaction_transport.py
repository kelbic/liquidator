"""Ключ провайдера режется и там, где URL РАСПАЛСЯ (находка ревьюера 09.08).

Фильтр журнала гейтился на «в строке есть ://» — но ошибки транспорта несут ключ БЕЗ схемы:
socket.gaierror печатает хост, urllib3/SSL — путь и параметры, requests в ряде отказов отдаёт
строку, где схемы нет вовсе. Гейт пропускал такую строку целиком, и ключ уходил в журнал
открытым — тот же канал утечки, который уже был закрыт один раз (#36) и открылся снова с
другой стороны.

Лечение двухрубежное: (1) литералы ключей, увиденные при разборе НАШИХ URL, режутся где угодно;
(2) прежний регексп по URL остаётся для того, чего мы ещё не видели.
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.rpc as rpc                                   # noqa: E402

KEY = "AbCdEf1234567890XYZsecretkey99"
URL = "https://lb.drpc.org/ogrpc?network=base&dkey=" + KEY

_sv = set(rpc._SECRETS)
try:
    rpc.note_secrets_from_url(URL)

    # 1. ГЛАВНОЕ: ошибка транспорта БЕЗ схемы — ключ обязан быть срезан
    for s in ("gaierror: lb.drpc.org unreachable dkey=" + KEY,
              "SSLError(1, 'certificate verify failed') for ogrpc?dkey=" + KEY,
              "Max retries exceeded with url: /ogrpc?network=base&dkey=" + KEY):
        out = rpc.redact(s)
        assert KEY not in out, "ключ уцелел в строке без схемы: %s" % out
        assert "<ключ скрыт>" in out, out

    # 2. КОНТРОЛЬ: обычный URL по-прежнему режется (прежний рубеж жив)
    out = rpc.redact("boom " + URL)
    assert KEY not in out and "lb.drpc.org" in out, out

    # 3. КОНТРОЛЬ: полезное НЕ режется — хост, хэши, адреса, числа блоков
    keep = "block 49666338 idx=2170 tx=0xdeadbeefcafe borrower 0xF0000000000000000000000000000000000000F0"
    assert rpc.redact(keep) == keep, rpc.redact(keep)

    # 4. ФИЛЬТР логгера: гейт «://» снят — строка без схемы обязана пройти через redact
    rec = logging.LogRecord("t", logging.INFO, __file__, 1,
                            "предиктор упал: dkey=%s host=lb.drpc.org", (KEY,), None)
    assert rpc.RedactingFilter().filter(rec) is True
    assert KEY not in (str(rec.msg) + str(rec.args)), (rec.msg, rec.args)

    # 5. СТРУКТУРНО: гейт не вернулся
    _src = pathlib.Path("chain/rpc.py").read_text()
    _flt = _src[_src.index("class RedactingFilter"):]
    assert '"://" in record.msg' not in _flt, "гейт «://» вернулся в фильтр — дыра открыта снова"

    # 6. Регистрация идёт из ЕДИНСТВЕННОЙ двери — конструктора BaseRpc
    assert "note_secrets_from_url(rpc_url)" in _src[_src.index("def __init__(self, rpc_url"):], \
        "BaseRpc больше не регистрирует секрет — новые контуры потекут"

    # 7. Короткие строки не режем (иначе порежем полезное)
    rpc.note_secret("short")
    assert rpc.redact("value short here") == "value short here"
finally:
    with rpc._SECRETS_LOCK:
        rpc._SECRETS.clear()
        rpc._SECRETS.update(_sv)

# 8. .gitignore закрывает ключи флота (один `git add -A` на хосте публиковал бы 110 приватников)
_gi = pathlib.Path(".gitignore").read_text()
assert "*keys*" in _gi and "*.key" in _gi, ".gitignore снова не закрывает ключи кошельков"

print("OK: ключ режется и без схемы, полезное цело, ключи кошельков вне git")
