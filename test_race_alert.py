"""Алерт вердикта судимой гонки (_pos_alert): человеку — первая строка эпизода и каждая
победа; слепой суд, форма без транзита и неармированный канал — молчание; отказ канала
не летит наверх (прибор сбоку от денежного пути)."""
import os
import unittest
from unittest import mock

from chain.spec_fire_live import SpecFireLive


VERDICT = ("ПОЗИЦИИ blk=50191661 [суд: владелец известен (2 ног)]: транзит idx=3068 "
           "tip=28000000 agg=0x0e3dc8a6 (полоса вперёд 1 tx, вся полоса 1); наших 70, "
           "в полосе [T+1,T+20]=0, tip-match=0, побед=0; наши idx 1152..3692 tip 1000000")
WIN = VERDICT.replace("побед=0", "побед=1")
NO_TRANSIT = ("ПОЗИЦИИ blk=50191661 [суд: владелец известен (2 ног)]: наших 70 "
              "(idx 1152..3692), транзита в блоке НЕТ, tip 1000000")


def _bare(alert=None):
    o = SpecFireLive.__new__(SpecFireLive)
    o._alert = alert
    return o


class RaceAlertTest(unittest.TestCase):
    def test_first_verdict_alerts_then_cooldown(self):
        sent = []
        o = _bare(lambda t, k: sent.append((t, k)))
        o._pos_alert(VERDICT, blind=False)
        o._pos_alert(VERDICT, blind=False)          # тот же эпизод — кулдаун глотает
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "race-verdict")
        self.assertIn("транзит idx=3068", sent[0][0])

    def test_win_pierces_cooldown(self):
        sent = []
        o = _bare(lambda t, k: sent.append((t, k)))
        o._pos_alert(VERDICT, blind=False)
        o._pos_alert(WIN, blind=False)              # победа — сквозь кулдаун
        self.assertEqual([k for _, k in sent], ["race-verdict", "race-win"])

    def test_blind_and_no_transit_silent(self):
        sent = []
        o = _bare(lambda t, k: sent.append((t, k)))
        o._pos_alert(VERDICT, blind=True)
        o._pos_alert(NO_TRANSIT, blind=False)
        o._pos_alert(None, blind=False)
        self.assertEqual(sent, [])

    def test_unarmed_channel_silent(self):
        _bare(alert=None)._pos_alert(VERDICT, blind=False)   # не бросает

    def test_env_kill_switch(self):
        sent = []
        o = _bare(lambda t, k: sent.append((t, k)))
        with mock.patch.dict(os.environ, {"SPEC_FIRE_RACE_ALERT": "0"}):
            o._pos_alert(VERDICT, blind=False)
        self.assertEqual(sent, [])

    def test_channel_exception_swallowed(self):
        def _boom(t, k):
            raise RuntimeError("канал упал")
        _bare(alert=_boom)._pos_alert(VERDICT, blind=False)  # не летит наверх


if __name__ == "__main__":
    unittest.main()
