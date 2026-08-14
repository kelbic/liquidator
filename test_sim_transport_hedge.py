"""Юниты хеджа preconf-сима (06.08): транспортный отказ preconf != вердикт по жертве.
Классификатор строк ошибок simulate_tx + дефолт флага. Без сети."""
import sys

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from chain.hotpath import _sim_transport_err, HOT_SIM_CONFIRMED_RETRY

# транспорт -> дочитка
for e in ("429 Client Error: Too Many Requests for url: https://mainnet-preconf.base.org",
          "HTTPSConnectionPool(host='x', port=443): Read timed out. (read timeout=10)",
          "Connection aborted.", "Max retries exceeded with url: /", "502 Bad Gateway",
          "503 Service Unavailable", "504 Gateway Time-out"):
    assert _sim_transport_err(e), e
print("UNIT_OK: транспортные ошибки классифицированы под дочитку")

# он-чейн вердикты -> НЕ дочитка (реверт правды не меняет)
for e in ("execution reverted", "execution reverted: SwapFailed",
          "execution reverted: 0x81ceff30", "Panic(0x11)", "", None):
    assert not _sim_transport_err(e), e
print("UNIT_OK: он-чейн реверты дочитку не дёргают")

assert HOT_SIM_CONFIRMED_RETRY is True
print("UNIT_OK: флаг HOT_SIM_CONFIRMED_RETRY включён по умолчанию (откат =0)")
print("ALL_OK: хедж сима — контракты выполнены")
