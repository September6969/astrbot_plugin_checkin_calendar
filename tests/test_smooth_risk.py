"""平滑风险模型的时间边界、单调性及显示精度。"""
import math
from datetime import timedelta
import pytest
from astrbot_plugin_checkin_calendar.store import CalendarStore,blast_probability
from astrbot_plugin_checkin_calendar.presentation import format_probability
from test_blast import NOW,seed


def test_midnight_and_72_hour_boundary_are_continuous(tmp_path):
    store=CalendarStore(tmp_path/'db');seed(store)
    for boundary in [NOW.replace(hour=0)+timedelta(days=1),NOW+timedelta(hours=72)]:
        before=store.risk('alice',boundary-timedelta(milliseconds=1)).risk_percent
        after=store.risk('alice',boundary+timedelta(milliseconds=1)).risk_percent
        assert 0 < after <= before
        assert before-after < .00001


def test_rest_monotonically_reduces_risk_without_date_reset(tmp_path):
    store=CalendarStore(tmp_path/'db');seed(store)
    values=[store.risk('alice',NOW+timedelta(hours=4+h)).risk_percent for h in range(168)]
    assert all(a>b for a,b in zip(values,values[1:]))
    assert values[-1]>0
    instant=NOW+timedelta(hours=4)
    from zoneinfo import ZoneInfo
    assert store.risk('alice',instant).risk_percent==store.risk('alice',instant.astimezone(ZoneInfo('America/Los_Angeles'))).risk_percent


def test_exponential_superposition_matches_independent_closed_form(tmp_path):
    store=CalendarStore(tmp_path/'db');seed(store)
    now=NOW+timedelta(hours=4)
    expected=80*(1-math.exp(-((.75*(2**(-2/6)+2**(-4/6))+.25*(2**(-2/36)+2**(-4/36)))/4)**2))
    assert store.risk('alice',now).risk_percent==pytest.approx(expected,abs=1e-12)
    store.change('alice','extra','add',now)
    assert store.risk('alice',now).risk_percent>expected


@pytest.mark.parametrize('value,label',[(0,'0%'),(.00001,'<0.1%'),(.1,'0.1%'),(11.01798,'11.0%'),(80,'80.0%')])
def test_probability_display_does_not_fake_zero(value,label):
    assert format_probability(value)==label
