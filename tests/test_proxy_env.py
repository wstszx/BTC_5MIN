from __future__ import annotations

import os

from proxy_env import bootstrap_proxy_environment


def test_bootstrap_proxy_environment_reads_machine_values_when_process_env_is_missing(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)

    def fake_registry_reader(scope: str, name: str) -> str | None:
        values = {
            ("machine", "HTTP_PROXY"): "http://127.0.0.1:7890",
            ("machine", "HTTPS_PROXY"): "http://127.0.0.1:7890",
        }
        return values.get((scope, name))

    applied = bootstrap_proxy_environment(registry_reader=fake_registry_reader)

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["http_proxy"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["https_proxy"] == "http://127.0.0.1:7890"
    assert applied["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert applied["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_bootstrap_proxy_environment_keeps_existing_process_values(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8888")

    def fake_registry_reader(scope: str, name: str) -> str | None:
        if scope == "machine" and name in {"HTTP_PROXY", "HTTPS_PROXY"}:
            return "http://127.0.0.1:7890"
        return None

    applied = bootstrap_proxy_environment(registry_reader=fake_registry_reader)

    assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:8888"
    assert os.environ.get("http_proxy") == "http://127.0.0.1:8888"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "HTTP_PROXY" not in applied
