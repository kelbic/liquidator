"""Своя победа не пишется конкурентом (#44) и несогласованность бакетов громкая (#53).

#44: победителем гонки может оказаться НАШ СОБСТВЕННЫЙ тракт — проба доски/движка/ноги берёт
жертву раньше боевой tx. Без тега такая строка уходила в эмпирическую tip-функцию конкурентов
(grep race-winner), в счётчик оспоренных гонок и в эскалацию tip: наша победа засчитывалась нам
же в поражение и ещё поднимала нам цену.

#53: дефолт дневного газ-капа ($100) меньше суммы бакетов проб ($40+$150) — выпадение
MAX_DAILY_GAS_USD из .env превращало САНКЦИОНИРОВАННЫЙ расход тира в остановку ВСЕГО боевого
огня посреди каскада. Правим не значение, а ОТСУТСТВИЕ СВЕРКИ: несогласованность обязана быть
громкой на старте, когда её ещё можно исправить.
"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                                # noqa: E402

CONTRACT = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
MAIN = "0xF0000000000000000000000000000000000000F0"
LEG = "0x" + "ab" * 20
cfg = types.SimpleNamespace(liquidator_address=CONTRACT, wallet_address=MAIN)

_sv = ex.volley_accounts
try:
    ex.volley_accounts = lambda: [(LEG, "0xkey")]

    # 1. наш контракт как получатель -> СВОЯ победа
    assert ex._is_ours("0xSomeoneElse", CONTRACT, cfg) is True
    # 2. нога флота как отправитель -> СВОЯ победа (регистр не важен)
    assert ex._is_ours(LEG.upper(), "0xOther", cfg) is True
    # 3. главный кошелёк -> своя
    assert ex._is_ours(MAIN, "0xOther", cfg) is True
    # 4. КОНТРОЛЬ: настоящий конкурент — НЕ своя (иначе тег бесполезен)
    assert ex._is_ours("0x455494aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                       "0x1f494779bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", cfg) is False
    # 5. неопределённость закрывается тихо и консервативно (не своя)
    assert ex._is_ours("?", "?", None) is False
finally:
    ex.volley_accounts = _sv

# 6. Брейкер: self-win не растит серию (как и race), но называется своим именем
ex._FL_CONSEC_REVERTS = 0
ex._FL_DISABLED = False
for _ in range(10):
    ex._fl_record(True, 0, 3, xray="self-win")
assert not ex.fast_lane_disabled() and ex._FL_CONSEC_REVERTS == 0, ex._FL_CONSEC_REVERTS
# КОНТРОЛЬ: настоящий шторм по-прежнему выключает
ex._FL_CONSEC_REVERTS = 0
ex._FL_DISABLED = False
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race")
assert ex.fast_lane_disabled(), "брейкер сломан — настоящий шторм обязан выключать"
ex._FL_CONSEC_REVERTS = 0
ex._FL_DISABLED = False

# 7. Структурно: вердикт и строка журнала различают свою победу, эскалация tip её не берёт
src = pathlib.Path("chain/execute.py").read_text()
assert 'return "self-win" if _self else "race"' in src, "вердикт self-win потерян"
assert '"self-win" if _self else "race-winner"' in src, "строка журнала не различает свою победу"
assert 'tip_s != "?" and not _self' in src, "своя победа снова эскалирует наш же tip"

# 8. #53: сверка капа с бакетами
assert ex.probe_budget_total() == ex._SPECFIRE_MAX_GAS_USD + ex.CASCADE_TIER_GAS_USD
assert ex.check_gas_budget_sane(ex.probe_budget_total() + 1) is True
assert ex.check_gas_budget_sane(ex.probe_budget_total()) is False, "кап РАВЕН бакетам — уже дыра"
assert ex.check_gas_budget_sane(0) is False and ex.check_gas_budget_sane(None) is False
_m = pathlib.Path("main.py").read_text()
assert "check_gas_budget_sane(cfg.max_daily_gas_usd" in _m, "сверка не зовётся на старте"

print("OK: своя победа тегирована и не эскалирует, сверка бакетов на старте работает")
