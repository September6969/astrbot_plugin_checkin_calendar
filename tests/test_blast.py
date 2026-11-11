"""炸膛概率、持久冷却和事务回归。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import pytest
from astrbot_plugin_checkin_calendar.store import CalendarStore, blast_probability

NOW=datetime(2026,11,10,8,tzinfo=ZoneInfo('Asia/Shanghai'))

def seed(store, count=2):
    from unittest.mock import patch
    with patch("astrbot_plugin_checkin_calendar.store.secrets.randbelow",return_value=999999):
        _seed(store, count)

def _seed(store, count):
    for i in range(count):
        store.change('alice',f'initial-{i}','add',NOW+timedelta(hours=2*i))


def test_probability_increases_with_both_loads_and_is_capped():
    assert blast_probability(0,0)==0
    assert 0 < blast_probability(1,1) < blast_probability(2,2) < blast_probability(3,3) < 80
    assert blast_probability(2,8)>blast_probability(2,2)
    assert blast_probability(100,100)==80


def test_blast_no_count_and_twelve_hours_persist_across_restart_undo_and_midnight(tmp_path,monkeypatch):
    store=CalendarStore(tmp_path/'db');seed(store)
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:0)
    now=NOW+timedelta(hours=4)
    result=store.change('alice','blast','add',now)
    assert result.status=='blasted' and result.risk_percent==pytest.approx(11.0179845844,abs=.001)
    assert datetime.fromisoformat(result.available_at)==now+timedelta(hours=12)
    assert store.month('alice','2026-11').total==2
    store.change('alice','undo','undo',now)
    restarted=CalendarStore(store.path)
    blocked=restarted.change('alice','blocked','add',now+timedelta(hours=12,seconds=-1))
    assert blocked.status=='blast_cooldown' and blocked.available_at==result.available_at
    assert restarted.change('alice','tz','add',now.astimezone(ZoneInfo('UTC'))).status=='blast_cooldown'
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:999999)
    assert restarted.change('alice','ready','add',now+timedelta(hours=12)).status=='added'


def test_duplicate_and_parallel_messages_only_roll_once(tmp_path,monkeypatch):
    store=CalendarStore(tmp_path/'db');seed(store)
    draw=Mock(return_value=0)
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',draw)
    now=NOW+timedelta(hours=4)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda i:CalendarStore(store.path).change('alice',str(i),'add',now),range(10)))
    assert sum(r.status=='blasted' for r in results)==1
    assert sum(r.status=='blast_cooldown' for r in results)==9
    draw.assert_called_once_with(1_000_000)
    index=next(i for i,r in enumerate(results) if r.status=='blasted')
    repeated=store.change('alice',str(index),'add',now+timedelta(days=2))
    assert repeated.replay and repeated.status=='blasted'
    draw.assert_called_once()
    assert store.month('alice','2026-11').total==2


def test_rolling_window_undo_and_status_are_readonly(tmp_path,monkeypatch):
    store=CalendarStore(tmp_path/'db');seed(store)
    store.change('alice','undo','undo',NOW+timedelta(hours=2))
    draw=Mock(side_effect=AssertionError('只读不应抽签'))
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',draw)
    state=store.risk('alice',NOW+timedelta(hours=4))
    assert state.today_count==2 and state.recent_count==2 and state.risk_percent==pytest.approx(11.0179845844,abs=.001)
    assert store.risk('alice',NOW+timedelta(hours=74,seconds=1)).recent_count==0
    assert store.risk('bob',NOW).risk_percent==0
    assert store.change('alice','early','add',NOW+timedelta(hours=2,minutes=1)).status=='cooldown'
    draw.assert_not_called()


def test_probability_boundary_and_failure_rollback(tmp_path,monkeypatch):
    store=CalendarStore(tmp_path/'db');seed(store)
    now=NOW+timedelta(hours=4)
    threshold=store.risk('alice',now).risk_percent
    import math
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:math.ceil(threshold*10000))
    assert store.change('alice','survive','add',now).status=='added'
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',Mock(side_effect=RuntimeError('模拟抽签故障')))
    with pytest.raises(RuntimeError): store.change('alice','retry','add',now+timedelta(hours=2))
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:0)
    assert store.change('alice','retry','add',now+timedelta(hours=2)).status=='blasted'


def test_recent_frequency_survives_midnight_then_decays(tmp_path,monkeypatch):
    store=CalendarStore(tmp_path/'db')
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:999999)
    previous=NOW-timedelta(days=1)
    for i in range(5): store.change('alice',str(i),'add',previous+timedelta(hours=2*i))
    state=store.risk('alice',NOW)
    assert (state.today_count,state.recent_count)==(0,5)
    assert state.risk_percent>0
    assert 0 < store.risk('alice',NOW+timedelta(days=4)).risk_percent < state.risk_percent
