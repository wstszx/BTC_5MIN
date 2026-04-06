from __future__ import annotations

from pathlib import Path

from config import build_config_from_env_values, load_env_file_values


def test_load_env_file_values_reads_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_bytes("COMMENT=\u4e2d\u6587\nMAX_STAKE=9.5\n".encode("gbk"))

    values = load_env_file_values(env_file)

    assert values == {"COMMENT": "\u4e2d\u6587", "MAX_STAKE": "9.5"}


def test_build_config_from_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_bytes(
        "TRADE_MODE=paper\nMAX_STAKE=9.5\nPOLYMARKET_FUNDER=\u4e2d\u6587\u5730\u5740\n".encode("gbk")
    )

    values = load_env_file_values(env_file)
    cfg = build_config_from_env_values(values)

    assert cfg.trade_mode == "paper"
    assert cfg.max_stake == 9.5
    assert cfg.live_funder == "\u4e2d\u6587\u5730\u5740"


def test_dashboard_assets_show_config_read_error_details():
    from dashboard import _dashboard_html, _dashboard_js

    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="cfgError"' in html
    assert 'function setConfigError(message)' in js
    assert "el('cfgError').textContent = message || '--';" in js
    assert "setConfigError(err && err.message ? err.message : '\u8bfb\u53d6\u914d\u7f6e\u5931\u8d25');" in js
    assert "setConfigError('--');" in js
