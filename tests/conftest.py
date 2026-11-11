"""普通业务测试固定抽签；概率与炸膛测试显式覆盖指定结果。"""
import pytest

@pytest.fixture(autouse=True)
def deterministic_blast_draw(monkeypatch):
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:n-1)
