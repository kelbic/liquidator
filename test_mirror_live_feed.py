"""Живое кольцо зеркала (21.08): tip транзита из СЫРОЙ tx кадра, а не из лага индекса.

Вскрытие эпизода BTC 11:33: класс tip съезжал 7.5→3.7 мгвей за минуты, кольцо кормилось
только полными сканами индекса (19% блоков) — tip-match=0 в 4/4 вердиктов при живом бёрсте.

Проверяется: декод tip из RLP всех типов tx (2/3 — поле, 1/legacy — gasPrice−basefee),
мусор -> None (незнание не кормит кольцо), и гарды дрейфа боевого хука (дедуп по (agg,bn),
ручка отката, свежий basefee)."""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from chain.feeds import tip_from_raw  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


def _rlp_item(v: int) -> bytes:
    if v == 0:
        return b"\x80"
    if v < 0x80:
        return bytes([v])
    body = v.to_bytes((v.bit_length() + 7) // 8, "big")
    return bytes([0x80 + len(body)]) + body


def _rlp_list(items: bytes) -> bytes:
    if len(items) < 56:
        return bytes([0xc0 + len(items)]) + items
    ln = len(items).to_bytes((len(items).bit_length() + 7) // 8, "big")
    return bytes([0xf7 + len(ln)]) + ln + items


def _tx2(chain_id, nonce, tip, tail=b"\x01\x02\x03" * 30):
    body = _rlp_item(chain_id) + _rlp_item(nonce) + _rlp_item(tip) + tail
    return "0x02" + (_rlp_list(body)).hex()


def _tx1(chain_id, nonce, gas_price, tail=b"\x00" * 40):
    body = _rlp_item(chain_id) + _rlp_item(nonce) + _rlp_item(gas_price) + tail
    return "0x01" + (_rlp_list(body)).hex()


def _tx_legacy(nonce, gas_price, tail=b"\x00" * 40):
    body = _rlp_item(nonce) + _rlp_item(gas_price) + tail
    return "0x" + (_rlp_list(body)).hex()


# ---- type-2: tip = третье поле ----
check("type-2: tip 1 мгвей", tip_from_raw(_tx2(8453, 5213, 1_000_000)) == 1_000_000)
check("type-2: tip 27.8 гвей (класс головы)", tip_from_raw(_tx2(8453, 0, 27_780_100_000)) == 27_780_100_000)
check("type-2: нулевой tip", tip_from_raw(_tx2(8453, 7, 0)) == 0)
check("type-2: длинный список (>55 байт)", tip_from_raw(_tx2(8453, 5213, 5_000_000, tail=b"\xaa" * 120)) == 5_000_000)

# ---- type-3 (blob): та же форма ----
raw3 = "0x03" + _tx2(8453, 1, 2_200_000)[4:]
check("type-3: поле читается", tip_from_raw(raw3) == 2_200_000)

# ---- type-1 и legacy: gasPrice − basefee ----
check("type-1: gasPrice-basefee", tip_from_raw(_tx1(8453, 3, 6_000_000), basefee=5_000_000) == 1_000_000)
check("type-1 без basefee -> None (незнание не кормит)", tip_from_raw(_tx1(8453, 3, 6_000_000)) is None)
check("legacy: gasPrice-basefee", tip_from_raw(_tx_legacy(9, 8_000_000), basefee=5_000_000) == 3_000_000)
check("legacy без basefee -> None", tip_from_raw(_tx_legacy(9, 8_000_000)) is None)

# ---- мусор ----
check("None -> None", tip_from_raw(None) is None)
check("пустая строка -> None", tip_from_raw("") is None)
check("не-hex -> None", tip_from_raw("0xzz") is None)
check("неизвестный тип 0x7f -> None", tip_from_raw("0x7f" + "00" * 30) is None)

# ---- гарды дрейфа боевого хука ----
import chain.hotpath as hp  # noqa: E402
_SRC = open(hp.__file__.replace(".pyc", ".py")).read()
check("хук стоит в ветке FORWARD_SEL", '("m", _dec[0], bn) not in feed_handled' in _SRC)
check("кольцо кормится ТОЛЬКО нашими агрегаторами (гейт agg_set, линза 21.08)",
      "_dec[0] in agg_set" in _SRC)
check("импорт note_transit_tip вне recv-петли",
      "note_transit_tip as _ntt_live   # живое кольцо" in _SRC)
check("дедуп помечается до вызова", 'feed_handled.add(("m", _dec[0], bn))' in _SRC)
check("basefee — свежий (_fresh_basefee), не переменная кадра",
      "tip_from_raw(_raw, basefee=_fresh_basefee())" in _SRC)
check("ручка отката MIRROR_LIVE_FEED есть", "MIRROR_LIVE_FEED" in _SRC and
      'os.environ.get("MIRROR_LIVE_FEED", "1")' in _SRC)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: живое кольцо — tip из кадра за ~200мс, незнание не кормит"))
sys.exit(1 if FAILS else 0)
