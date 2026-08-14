"""Статическая проверка: в chain/ нет вызовов по имени, которого не существует.

Повод (07.08). `chain/execute.py:send_board_tx` обращался к `_fresh_basefee()`, а функция живёт
в `chain/hotpath.py`; пять других вызывающих делают локальный импорт, этот один — нет. Итог:
КАЖДЫЙ арм v5-доски падал в NameError, `board_arms` за всю историю журнала равен нулю, залп
молча откатывался на V1 с полной калдатой. Прожило это пять суток по трём причинам сразу:

  * путь исполняется только в бою, на редком событии (арм доски — 5 раз за 30 суток журнала);
  * исключение ловилось и печаталось как `type(e).__name__` — «NameError» без текста читается
    как осечка, а не как «этой переменной нет»;
  * юнит-тесты подменяют шов `_board_arm_fn` заглушкой, поэтому настоящий `send_board_tx`
    не исполнялся ни в одном из 38 зелёных тестов.

Поэтому проверка здесь СТАТИЧЕСКАЯ и по всему пакету: она не зависит от того, дошло ли
исполнение до строки. Ловится класс дефекта, а не конкретная строка.

Как это работает. У объекта кода имена глобалов, к которым он обращается, лежат в co_names
(опкод LOAD_GLOBAL). Локальный импорт в теле функции (`from chain.hotpath import _fresh_basefee`)
компилируется в STORE_FAST, то есть имя становится ЛОКАЛЬНЫМ и в co_names не попадает — принятая
в проекте идиома ложных срабатываний не даёт. Остаётся сверить каждое имя с globals() модуля
и builtins.
"""
import builtins
import dis
import importlib
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

PKG = pathlib.Path(__file__).parent / "chain"
BUILTINS = set(dir(builtins))

# Имена, создаваемые в рантайме и невидимые статически. Список ОБЯЗАН оставаться коротким и
# именным: «глушилка по подстроке» превратила бы проверку в украшение.
ALLOW = {
    "__file__", "__name__", "__doc__", "__package__", "__loader__", "__spec__",
    "__builtins__", "__annotations__", "__path__",
}


def _codes(obj):
    """Объект кода и все вложенные (методы, вложенные функции, comprehension-и, лямбды)."""
    yield obj
    for c in obj.co_consts:
        if isinstance(c, types.CodeType):
            yield from _codes(c)


def _globals_used(code):
    """Имена, читаемые как ГЛОБАЛЬНЫЕ. Только чтение: LOAD_GLOBAL. Присваивания (STORE_GLOBAL)
    имя определяют, а не требуют, и в счёт не идут."""
    out = set()
    for ins in dis.get_instructions(code):
        if ins.opname == "LOAD_GLOBAL" and isinstance(ins.argval, str):
            out.add(ins.argval)
    return out


def check_module(modname):
    mod = importlib.import_module(modname)
    defined = set(vars(mod)) | BUILTINS | ALLOW
    missing = {}
    for code in _codes(mod.__dict__["__loader__"].get_code(modname)):
        for name in _globals_used(code):
            if name not in defined:
                missing.setdefault(name, []).append(code.co_name)
    return missing


def main():
    mods = sorted("chain." + p.stem for p in PKG.glob("*.py")
                  if p.stem != "__init__" and not p.stem.startswith("_"))
    assert mods, "не нашёл ни одного модуля в chain/ — проверка смотрела бы в пустоту"

    bad, skipped = {}, []
    for m in mods:
        try:
            found = check_module(m)
        except Exception as e:
            # Модуль не импортируется — это отдельная беда, но молчать о ней нельзя:
            # непроверенный модуль обязан быть НАЗВАН, иначе зелёный прогон соврёт о покрытии.
            skipped.append("%s (%s: %s)" % (m, type(e).__name__, str(e)[:80]))
            continue
        if found:
            bad[m] = found

    print("проверено модулей: %d" % (len(mods) - len(skipped)))
    if skipped:
        print("НЕ ПРОВЕРЕНЫ (не импортируются): %s" % ", ".join(skipped))
    for m, names in sorted(bad.items()):
        for name, where in sorted(names.items()):
            print("  %s: имя %r не определено; читают: %s" % (m, name, ", ".join(sorted(set(where)))))

    assert not bad, "неразрешимые имена в chain/ — см. список выше"

    # ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: проверка обязана ловить ровно тот дефект, ради которого написана.
    # Без него «0 находок» неотличимо от «искали не то» — ровно так и прожил NameError.
    probe = types.ModuleType("probe")
    exec(compile("def f():\n    return _fresh_basefee()\n", "<probe>", "exec"), probe.__dict__)
    used = set()
    for code in _codes(compile("def f():\n    return _fresh_basefee()\n", "<probe>", "exec")):
        used |= _globals_used(code)
    assert "_fresh_basefee" in used, "контроль: проверка не видит вызов необъявленного имени"

    # и обратный: принятая идиома локального импорта НЕ должна давать ложную тревогу
    src = "def f():\n    from chain.hotpath import _fresh_basefee\n    return _fresh_basefee()\n"
    used2 = set()
    for code in _codes(compile(src, "<probe2>", "exec")):
        used2 |= _globals_used(code)
    assert "_fresh_basefee" not in used2, "контроль: локальный импорт даёт ложное срабатывание"

    print("OK: неразрешимых имён нет; оба контроля сошлись")


if __name__ == "__main__":
    main()
