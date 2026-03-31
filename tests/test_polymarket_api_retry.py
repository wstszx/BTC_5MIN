import requests

from config import AppConfig
from polymarket_api import PolymarketClient


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FlakySession:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            raise requests.exceptions.SSLError("transient ssl")
        return _StubResponse({"ok": True})


class _AlwaysFailSession:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        raise requests.exceptions.ConnectionError("network down")


def test_get_json_retries_with_backoff_and_recovers(monkeypatch):
    sleeps = []
    monkeypatch.setattr("polymarket_api.time.sleep", lambda seconds: sleeps.append(seconds))

    client = PolymarketClient(AppConfig(), session=_FlakySession())
    payload = client._get_json("/ping", base_url="https://example.com", retries=3)

    assert payload == {"ok": True}
    assert client.session.calls == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]


def test_get_json_raises_runtime_error_after_retries(monkeypatch):
    monkeypatch.setattr("polymarket_api.time.sleep", lambda _seconds: None)
    client = PolymarketClient(AppConfig(), session=_AlwaysFailSession())

    try:
        client._get_json("/events", base_url="https://example.com", retries=2)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

    assert "Unable to fetch https://example.com/events" in message
    assert "after 2 attempts" in message
    assert client.session.calls == 2
