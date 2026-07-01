"""Точечный чек: кто (если кто-то) ликвидировал заёмщика 0x4Ffc5F22... на market_id
0x67ebd84b2fb39e3bc5a13d97e4c07abe1ea617e40654826e9abce252e95f049e вокруг блока 48039561.
Read-only, разовый, не трогает hot path. Различает:
  (а) конкурент опередил -> латентность (включая Kyber-часть на этом не-money-market) релевантна
  (б) никто не ликвидировал -> флип исчез сам (цена откатилась), Kyber ни при чём для ЭТОГО случая
Запуск:  cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) \
         /root/liquidator/venv/bin/python -m analysis.borrower_liq_check
"""
import sys
sys.path.insert(0, ".")
from config import Config
from analysis.competition_report import _gql

cfg = Config.from_env()

MARKET_ID = "0x67ebd84b2fb39e3bc5a13d97e4c07abe1ea617e40654826e9abce252e95f049e"
BORROWER_PREFIX = "0x4ffc5f22"  # усечённый префикс из лога (b[:10]), нижний регистр для сравнения
TARGET_BLOCK = 48039561

Q = """query($mid:[String!]!,$cid:[Int!]!){
  marketTransactions(first:20,orderBy:Timestamp,orderDirection:Desc,
    where:{marketUniqueKey_in:$mid,type_in:[Liquidation],chainId_in:$cid}){
    items{ timestamp blockNumber txHash user{address}
      data{ ... on MarketTransactionLiquidationData{ liquidator repaidAssets seizedAssets } } } } }"""

r = _gql(Q, {"mid": [MARKET_ID], "cid": [cfg.chain_id]})
if r.get("errors"):
    print("API errors:", r["errors"])
    sys.exit(1)

items = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
print(f"Найдено {len(items)} ликвидаций на этом рынке (последние 20 по времени):\n")

our = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
match = None
for it in items:
    user_addr = (it.get("user") or {}).get("address", "") or ""
    bn = it.get("blockNumber")
    d = it.get("data") or {}
    liq = d.get("liquidator", "") or ""
    print(f"  ts={it.get('timestamp')} block={bn} user={user_addr} liquidator={liq} txHash={it.get('txHash')}")
    if user_addr.lower().startswith(BORROWER_PREFIX):
        match = it

print()
if match:
    bn = match.get("blockNumber")
    liq = (match.get("data") or {}).get("liquidator", "") or ""
    diff = f"{bn - TARGET_BLOCK}" if isinstance(bn, int) else "?"
    print(f"НАЙДЕНО: заёмщик {BORROWER_PREFIX}... ликвидирован в блоке {bn} "
          f"(наша попытка была в блоке {TARGET_BLOCK}, разница {diff} блок(ов))")
    print(f"liquidator = {liq}")
    if liq.lower() in our:
        print("=> это НАШ адрес — не сходится с submitted:0 в логе, требует отдельной проверки")
    else:
        print("=> (а) КОНКУРЕНТ опередил — латентность (включая Kyber-котировку на этом "
              "не-money-market) релевантна для этого проигрыша")
else:
    print(f"НЕ НАЙДЕНО заёмщика {BORROWER_PREFIX}... среди последних {len(items)} ликвидаций "
          f"этого рынка.")
    print("=> (б) похоже, позицию никто не ликвидировал — флип исчез сам (цена восстановилась), "
          "Kyber-задержка нерелевантна для ЭТОГО конкретного случая")
