"""Юниты стартовой пробы подписи (#COIN): чем и почём бот подписывает.

ЗАЧЕМ ОНА. Боевой venv Шарлотты отдавал NativeECCBackend (чистый Python) — coincurve не был
установлен вовсе. Правка requirements.txt сама по себе на машину НЕ доезжает: нужен пересбор
venv. Значит факт обязан быть в ЖУРНАЛЕ БОТА, иначе «поставили» непроверяемо («хэш tx — не
эффект»).

ЧТО ЗАЩИЩАЕТ ЭТОТ ФАЙЛ (каждый пункт — отдельный класс регрессии):
  1. строка печатается НАСТОЯЩИМ стартом main.py (не вызовом функции в тесте: шов подменён
     выше дефекта — если из main() уберут вызов, тест обязан покраснеть);
  2. деградация в native ГРОМКАЯ: уровень WARNING и слово «МЕДЛЕННЫЙ», а не тихий INFO;
  3. подтверждение бэкенда — по СЧЁТЧИКУ вызовов ecdsa_sign, а не по имени класса
     (мутация: подпись не идёт через бэкенд -> confirmed=False);
  4. на быстром бэкенде строка INFO и fast=True;
  5. проба не имеет права ронять старт и не имеет права читать боевой ключ.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = "eth_keys.backends.native.main.NativeECCBackend"
COINCURVE = "eth_keys.backends.coincurve.CoinCurveECCBackend"


def _has_coincurve():
    try:
        import coincurve  # noqa: F401
        return True
    except Exception:
        return False


def _probe(backend_class, extra_code=""):
    """Запускаем пробу в ОТДЕЛЬНОМ процессе с пиненным бэкендом: выбор бэкенда eth_keys делает
    один раз на импорт, внутри одного процесса его не переиграть честно."""
    code = ("import json, logging, sys\n"
            "sys.path.insert(0, %r)\n"
            "logging.basicConfig(level=logging.INFO, format='%%(levelname)s %%(message)s')\n"
            "import chain.execute as ex\n"
            + extra_code +
            "log = logging.getLogger('t')\n"
            "r = ex.sign_backend_probe(warmup=8, log=log)\n"
            "print('JSON' + json.dumps(r))\n") % HERE
    env = dict(os.environ, ECC_BACKEND_CLASS=backend_class)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                       timeout=180)
    assert p.returncode == 0, "проба уронила процесс:\n%s" % p.stderr[-2000:]
    j = [l for l in p.stdout.splitlines() if l.startswith("JSON")]
    assert j, "проба не вернула результат:\n%s\n%s" % (p.stdout[-800:], p.stderr[-800:])
    return json.loads(j[-1][4:]), p.stderr


# ---- 1. НАСТОЯЩИЙ БЭКЕНД: имя, p50, подтверждение вызовом --------------------------------
r_nat, err_nat = _probe(NATIVE)
assert r_nat["backend"] == NATIVE, r_nat
assert r_nat["confirmed"] is True, "счётчик ecdsa_sign не сработал: %s" % r_nat
assert r_nat["n"] >= 8 and r_nat["p50_ms"] > 0, r_nat
print("UNIT_OK 1: native подтверждён вызовами, p50=%.3fмс (n=%d)" % (r_nat["p50_ms"], r_nat["n"]))

# ---- 2. ДЕГРАДАЦИЯ ГРОМКАЯ: native => WARNING, а не тихий INFO ---------------------------
_line = [l for l in err_nat.splitlines() if "SIGN бэкенд=" in l]
assert _line, "стартовая строка не напечатана вовсе:\n%s" % err_nat[-800:]
assert _line[0].startswith("WARNING"), "native обязан быть WARNING, а не «%s»" % _line[0][:40]
assert "МЕДЛЕННЫЙ" in _line[0] and "requirements.txt" in _line[0], _line[0]
assert r_nat["fast"] is False
print("UNIT_OK 2: без coincurve строка ГРОМКАЯ (WARNING + рецепт лечения)")

# ---- 3. МУТАЦИЯ: подпись мимо бэкенда => confirmed ОБЯЗАН упасть -------------------------
# Позитивный контроль самого прибора: если бы confirmed читался с имени класса, эта мутация
# осталась бы незамеченной и строка врала бы «подтверждён=да» на мёртвом бэкенде.
_mut = ("import eth_account.account as _aa\n"
        "class _Fake:\n"
        "    r=1; s=2; v=0; hash=b'\\x00'*32; raw_transaction=b'\\x02'\n"
        "_aa.LocalAccount.sign_transaction = lambda self, tx: _Fake()\n")
r_mut, err_mut = _probe(NATIVE, _mut)
assert r_mut["confirmed"] is False, "мутация не поймана — confirmed читается не с эффекта: %s" % r_mut
assert "подтверждён=НЕТ" in err_mut, err_mut[-500:]
print("UNIT_OK 3: подпись мимо бэкенда => подтверждён=НЕТ (счётчик, а не имя класса)")

# ---- 4. БЫСТРЫЙ ПУТЬ: coincurve => INFO, fast=True, и он РЕАЛЬНО быстрее -----------------
if _has_coincurve():
    r_cc, err_cc = _probe(COINCURVE)
    assert r_cc["backend"] == COINCURVE and r_cc["confirmed"] and r_cc["fast"], r_cc
    _lc = [l for l in err_cc.splitlines() if "SIGN бэкенд=" in l]
    assert _lc and _lc[0].startswith("INFO"), "быстрый путь не обязан кричать: %s" % (_lc or err_cc)
    assert "МЕДЛЕННЫЙ" not in _lc[0]
    # ВЕРСИЯ — из метаданных. У coincurve 20.0.0 (боевая сборка Шарлотты под Python 3.14)
    # атрибута __version__ НЕТ: чтение через getattr печатало бы «coincurve=?» именно там,
    # где строку и читают. Тест краснеет, если версия снова станет неизвестной.
    import importlib.metadata as _md
    assert r_cc["coincurve"] == _md.version("coincurve"), \
        "версия coincurve в строке старта не совпала с метаданными: %r против %r" \
        % (r_cc["coincurve"], _md.version("coincurve"))
    assert "?" not in str(r_cc["coincurve"]), r_cc["coincurve"]
    _ratio = r_nat["p50_ms"] / r_cc["p50_ms"]
    assert _ratio > 2.0, ("coincurve не даёт ускорения (x%.2f) — либо бэкенд не тот, "
                          "либо замер сломан" % _ratio)
    print("UNIT_OK 4: coincurve INFO, p50=%.3fмс против native %.3fмс = x%.2f"
          % (r_cc["p50_ms"], r_nat["p50_ms"], _ratio))
else:
    print("SKIP 4: coincurve не установлен в ЭТОМ окружении — ветка быстрого пути не проверена")

# ---- 5. ПРОБА НЕ РОНЯЕТ СТАРТ ------------------------------------------------------------
r_bad, err_bad = _probe(NATIVE, "import eth_account.account as _aa\n"
                                "def _boom(self, tx): raise RuntimeError('сломано')\n"
                                "_aa.LocalAccount.sign_transaction = _boom\n")
assert r_bad.get("error"), "исключение проглочено без диагноза: %s" % r_bad
assert r_bad["confirmed"] is False
print("UNIT_OK 5: отказ пробы не роняет старт и несёт причину (%s)" % r_bad["error"][:40])

# ---- 6. ГЛАВНОЕ: строку печатает НАСТОЯЩИЙ старт main.py ---------------------------------
# Иначе тест защищал бы функцию, которую из main() уже вынули. Поднимаем боевую точку входа
# в monitor-режиме без RPC, ловим строку из journal-потока и гасим процесс.
tmp = tempfile.mkdtemp(prefix="signprobe-")
_cm = os.path.join(tmp, "covered.json")
open(_cm, "w").write("[]")
env = dict(os.environ, ECC_BACKEND_CLASS=NATIVE, MODE="monitor", RPC_URL="",
           DB_PATH=os.path.join(tmp, "t.db"), COVERED_MARKETS_PATH=_cm,
           TG_BOT_TOKEN="", TG_ADMIN_ID="", LOOP_MODE="poll", POLL_INTERVAL_SEC="1",
           WALLET_KEY="", PYTHONUNBUFFERED="1")
p = subprocess.Popen([sys.executable, "main.py"], cwd=HERE, env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
found = None
try:
    import time as _t
    _t0 = _t.time()
    while _t.time() - _t0 < 60:
        line = p.stdout.readline()
        if not line:
            break
        if "SIGN бэкенд=" in line:
            found = line.strip()
            break
finally:
    p.kill()
    p.wait(timeout=20)
assert found, "БОЕВОЙ старт main.py НЕ печатает строку подписи — вызов вынут из точки входа"
assert re.search(r"SIGN бэкенд=\S+ p50=[\d.]+мс \(\d+ подписей прогрева\)", found), found
print("UNIT_OK 6: строку печатает НАСТОЯЩИЙ main.py: %s" % found[:150])

print("ПРОШЛИ: стартовая проба ECC-бэкенда (#COIN)")
