# WORKFLOW.md — рабочий контракт (liquidator)

Тот же контур, что держит twidgest/essayist в проде без откатов, плюс слой денежной
безопасности (этот бот, в отличие от контентных, может потерять деньги). Не упрощать.

---

## Два класса изменений

- **Прод-код бота** (`main.py`, `chain/*`, `strategy/*`, `config.py`, `store.py`, контракт) —
  ПОЛНЫЙ контур ниже: `.bak` → patcher c assert-счётчиками → новые файлы целиком →
  блок проверок → деплой/рестарт/grep. Для execute-пути — плюс денежные гейты.
- **Read-only / research** (скрипты `analysis/*`, пробы, замеры — не правят прод-файлы, не
  трогают сервис, не используют ключ) — ЛЁГКИЙ путь: import-валидация + прогон на VPS, без
  `.bak`/patcher/рестарта. Так делались `preliq_inventory` / `preliq_optin_probe`.

---

## Формат одного шага (прод-код)

Один логический шаг = одно сообщение:
1. `.bak`-бэкап затрагиваемых файлов (с датой).
2. Patcher-heredoc с `assert t.count(old) == 1` на каждый якорь (падает громко).
3. Новые файлы целиком — **в том же сообщении**.
4. Блок проверок: компиляция + рантайм-импорт + AST (если правка трогает имена/атрибуты)
   + быстрый юнит для любой чистой функции, с явным «ожидаю: …».
5. Коммит + push + рестарт сервиса + grep на ошибки.

Владелец выполняет всё разом и присылает вывод. По выводу — решение.

---

## Клон и синхронизация

Клон: `/root/_clone_liq` (стабильнее `/tmp` — он вычищается).
```bash
cd /root/_clone_liq && git checkout -q . && git clean -qfd 2>/dev/null; git pull -q && git log --oneline -5
```
Если клон исчез — `git clone https://github.com/kelbic/liquidator /root/_clone_liq`.
Dry-run прод-кода — на клоне. **Никогда** не гонять dry-run в `MODE=execute` с реальным
`WALLET_KEY`: клон тестировать в `MODE=monitor` или с пустым/тестовым ключом.

---

## Patcher — канонический вид (+ шаг 0)

**Шаг 0 (обязателен):** перед написанием якоря сверить РЕАЛЬНЫЙ текст прод-файла —
песочница/клон дрейфуют от прода (был случай: статус-строка не дошла до прод-`main.py`,
якоря из клона не совпали):
```bash
cd /root/liquidator && grep -n "<кусок якоря>" main.py && sed -n '<N>,<M>p' main.py | cat -A
```
Якорь брать из ЭТОГО вывода, не из памяти/клона. Якоря — точечные, БЕЗ хвостовых комментариев.

```python
"""Что делает патч (dry-run на клоне <HASH>)."""
from pathlib import Path
import os

os.chdir("/root/liquidator")
p = Path("path/to/file.py"); t = p.read_text(encoding="utf-8")
old = """<уникальный якорь из cat -A>"""
new = """<замена>"""
assert t.count(old) == 1, "FAIL <метка>"
p.write_text(t.replace(old, new), encoding="utf-8")
print("OK  path/to/file.py")
```
Якорь встречается дважды — расширить контекстом, пока `count == 1`.

---

## Env-заглушки для рантайм-импорта

`config.py` импорт-безопасен (`Config.from_env` читает env лениво), `chain/*` импортируют
web3 ЛЕНИВО — базовый импорт чист без заглушек:
```bash
python3 -c "import main; print('RUNTIME OK')"
```
Под реальный прогон (не импорт) минимальный env:
```bash
RPC_URL=x WALLET_ADDRESS=0x0 MODE=monitor TG_BOT_TOKEN= TG_ADMIN_ID=0 \
python3 -c "import config; config.Config.from_env(); print('CONFIG OK')"
```

---

## AST-проверка — обязательна при правке имён/атрибутов

`py_compile`/`import` НЕ ловят три вещи. Шаблоны прогонять на затронутом файле.

**(1) Локальный импорт, затеняющий модульное имя** (код выше импорта падает с
`UnboundLocalError` — Python делает имя локальным на этапе компиляции функции). Самый
опасный класс для денежного бота: молча вооружает сломанный путь. Чистый stdlib, без зависимостей:
```bash
python3 << 'EOF'
import ast
src = open("chain/execute.py").read()        # <- затронутый файл
tree = ast.parse(src)
modlevel = set()
for n in tree.body:
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            modlevel.add(a.asname or a.name.split(".")[0])
bad = []
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Import, ast.ImportFrom)):
                for a in inner.names:
                    nm = a.asname or a.name.split(".")[0]
                    if nm in modlevel:
                        bad.append((node.name, nm))
assert not bad, ("локальный импорт затеняет модульное имя:", bad)
print("AST OK: затенения нет")
EOF
```

**(2) Обращение к несуществующему полю dataclass** (AttributeError только в рантайме —
напр. `ctx.total_borrow_asset` вместо `...assets`). VARMAP — по соглашению об именах в файле:
```bash
python3 << 'EOF'
import ast, dataclasses
from chain.simulate import SimResult, MarketContext, HealthReport
from chain.morpho import Position
from strategy.pnl import PnlInputs
DCLS = {c.__name__: c for c in (SimResult, MarketContext, HealthReport, Position, PnlInputs)}
VARMAP = {"ctx": "MarketContext", "hr": "HealthReport", "sim": "SimResult",
          "pos": "Position", "pnl": "PnlInputs"}     # <- под имена в файле
src = open("chain/simulate.py").read()               # <- затронутый файл
fields = {k: {f.name for f in dataclasses.fields(v)} for k, v in DCLS.items()}
bad = []
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        cls = VARMAP.get(node.value.id)
        if cls and not node.attr.startswith("__") and node.attr not in fields[cls]:
            bad.append((node.value.id, cls, node.attr))
assert not bad, ("несуществующее поле dataclass:", bad)
print("DATACLASS ATTRS OK")
EOF
```

**(3) Имя без импорта внутри функции** — ловится рантайм-импортом (раздел выше) либо
`python3 -m pyflakes <файл>` (если установлен).

Правка добавляет чистую функцию (расчёт/форматирование) — добавить юнит прямо в блок проверок.

---

## Миграции SQLite

Перед `ALTER TABLE` — бэкап с датой:
```bash
cp liquidator.db "pre-<что>-$(date +%Y%m%d-%H%M).db"
sqlite3 liquidator.db "ALTER TABLE ...;"
```
Колонку — И в БД, И в `SCHEMA` (`store.py`).

---

## Слой денежной безопасности (новое vs контентные боты)

- **Тестнет первым.** Логика отправки — на Base Sepolia (`CHAIN_ID=84532`) до mainnet.
- **Симуляция-перед-отправкой = on-chain dry-run.** Каждая реальная ликвидация гейтится
  успешной `eth_call`/форк-симуляцией с net ≥ `MIN_PROFIT_USD`. Реверт/минус → НЕ шлём.
- **Kill switch.** `MAX_DAILY_LOSS_USD`/`MAX_DAILY_GAS_USD`/`MAX_INFLIGHT`; пробой → бот
  встаёт + Telegram-алерт (антифлуд). В monitor гварды тоже считаются (логируем «встали бы»).
- **`.env` перебивает dataclass-дефолт** (`EnvironmentFile` в systemd). Меняешь значение →
  правишь И `config.py` (дефолт), И `.env` (sed). Прод-`.env`: `MAX_INFLIGHT=5`. Иначе правка
  дефолта в коде молча не доходит до прода.
- **Ключ.** `WALLET_KEY` только в `.env` (chmod 600), не коммитим, ассистенту не передаём.
  Команды читают из `.env` ТОЛЬКО `RPC_URL` (без печати), ключ не трогают. Средства — газ.
- **Ресурс-чек.** После деплоя `systemd-cgtop` + `systemctl show liquidator-bot
  -p CPUWeight,MemoryMax,CPUQuota` — капы применились, twidgest не задет.
- **Рестарт вооружённого бота** стоит ~секунд простоя (пропуск блоков); любая правка
  execute/dispatch-пути — выше ставка, форвард-фикс осторожнее.

---

## Деплой и контроль

```bash
git add <files>
git commit -m "<императив: что и зачем>"
git push
systemctl restart liquidator-bot && sleep 8 && systemctl is-active liquidator-bot
journalctl -u liquidator-bot --since "-2 min" --no-pager | grep -cE "ERROR|Traceback"
systemd-cgtop -b -n1 | grep -E "liquidator-bot|twidgest-bot"
rm -f <bak-файлы>
```
Здоровый финал: `active` + `0` ошибок + ликвидатор не давит twidgest. `activating`
(не `active`) — crash-loop: читать `journalctl`, форвард-фикс или откат из `.bak`.

---

## Если что-то упало в проде

1. `journalctl -u liquidator-bot --since "<окно>" --no-pager | sed -n '/Traceback/,/INFO/p'`.
2. Воспроизвести на клоне (monitor), починить, dry-run, деплой форвард-фиксом.
3. Откат из `.bak` — крайняя мера.

Помни про часовой сдвиг VPS относительно времени клиента при выборе окна `--since`
(Telegram — клиентское, `journalctl` — серверное).
