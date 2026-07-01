"""Синтетический стресс-тест гипотезы A: гонит РЕАЛЬНЫЙ read_positions (тот же, что горячий путь)
по РЕАЛЬНОМУ текущему списку кандидатов (fetch_candidates — та же функция, что вызывает
_refresh_worker) много раз подряд, БЕЗ ожидания реального флипа/каскада. Если aggregate3 иногда
роняет отдельные position()-подзвонки под реалистичным размером батча — DIAG read_positions drop
(уже встроен в read_positions с cc6ec34) всплывёт сам собой, никакой новой логики детекции не
нужно. Тестирует МЕХАНИЗМ (размер батча + поведение RPC-провайдера), не зависит от того, есть ли
сейчас реальная ликвидируемая позиция. Read-only, не трогает hot path, не отправляет транзакций.
Запуск: cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) \
        /root/liquidator/venv/bin/python -m analysis.stress_read_positions [n_passes]
"""
import sys, logging, time
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from config import Config
from chain.rpc import BaseRpc
from chain.hotpath import read_positions
from main import fetch_candidates, load_covered_markets

cfg = Config.from_env()
rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
preconf_rpc = BaseRpc(cfg.preconf_rpc or "https://mainnet-preconf.base.org", cfg.chain_id)

n_passes = int(sys.argv[1]) if len(sys.argv) > 1 else 10

markets = load_covered_markets(cfg.covered_markets_path)
print(f"Покрытых рынков: {len(markets)}")

groups, debt_by, debt_assets_by, ncand = fetch_candidates(markets)
print(f"Кандидатов всего: {ncand} по {len(groups)} рынкам")
print(f"Прогонов: {n_passes} полных проходов по всем рынкам\n")

total_calls = 0
total_borrowers_attempted = 0
drops_seen = 0
t0 = time.time()
for i in range(n_passes):
    for mid, borrowers in groups.items():
        if not borrowers:
            continue
        try:
            res = read_positions(rpc, mid, borrowers, preconf_rpc=preconf_rpc)
            if res is None:
                continue
            tba, tbs, pos = res
            total_calls += 1
            total_borrowers_attempted += len(borrowers)
            if len(pos) < len(borrowers):
                drops_seen += (len(borrowers) - len(pos))
        except Exception as e:
            print(f"  ошибка на market={mid[:10]}: {type(e).__name__}: {str(e)[:80]}")
    print(f"проход {i+1}/{n_passes} завершён ({time.time()-t0:.1f}с суммарно)")

print(f"\nИТОГ: {total_calls} вызовов read_positions, {total_borrowers_attempted} заёмщик-попыток,")
print(f"{drops_seen} дропов замечено по возврату (см. также строки 'DIAG read_positions drop' "
      f"выше в этом же выводе — та же метрика, но по конкретному адресу заёмщика)")
