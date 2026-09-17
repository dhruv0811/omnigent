"""E2E regression test: the codex model-probe home must track its source credential.

The persistent probe home (``~/.omnigent/cache/codex-model-probe/<key>``) is
keyed on the ``-c`` overrides alone, so a ``CODEX_HOME`` switch with identical
overrides reuses the same cache directory. The bridged ``config.toml`` is
refreshed on every probe; the credential symlink must be too, or it dangles at
the old source home and every later probe answers as a logged-out account.

This drives the real probe (``probe_codex_model_options`` boots
``codex app-server``) twice: once against the original source home, once after
the user moves it.

Run::

    OMNIGENT_E2E_CODEX_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_codex_model_probe_home_credential_refresh_e2e.py -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import (
    NativeCodexLaunch,
    probe_codex_model_options,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason="requires OMNIGENT_E2E_CODEX_NATIVE=1 and the codex CLI on PATH",
)


def _fake_chatgpt_auth() -> str:
    """A parseable subscription-login ``auth.json`` (offline; never validated)."""

    def b64(payload: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    token = ".".join(
        (
            b64({"alg": "none", "typ": "JWT"}),
            b64(
                {
                    "exp": int(time.time()) + 86400,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "pro",
                        "chatgpt_account_id": "acct_test",
                    },
                    "email": "probe-test@example.com",
                }
            ),
            "",
        )
    )
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": token,
                "access_token": token,
                "refresh_token": "fake",
                "account_id": "acct_test",
            },
            "last_refresh": "2026-01-01T00:00:00Z",
        }
    )


def _write_source_codex_home(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "config.toml").write_text('model = "gpt-5.1-codex"\n')
    (path / "auth.json").write_text(_fake_chatgpt_auth())


def _probe(monkeypatch: pytest.MonkeyPatch, codex_home: Path) -> list[dict[str, Any]]:
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    launch = NativeCodexLaunch(config_overrides=[], model=None, profile=None)
    return asyncio.run(probe_codex_model_options(launch=launch))


def _probe_home_credential(home: Path) -> Path:
    probe_root = home / ".omnigent" / "cache" / "codex-model-probe"
    keys = [entry for entry in probe_root.iterdir() if entry.is_dir()]
    assert len(keys) == 1, f"expected exactly one probe home, found {keys}"
    return keys[0] / "auth.json"


def test_probe_home_credential_follows_a_moved_source_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source_a = home / "codex-a"
    _write_source_codex_home(source_a)

    rows = _probe(monkeypatch, source_a)
    assert rows, "first probe returned no models"
    credential = _probe_home_credential(home)
    assert credential.exists(), "first probe bridged no credential into the probe home"
    assert credential.resolve() == (source_a / "auth.json").resolve()

    source_b = home / "codex-b"
    shutil.move(source_a, source_b)

    rows = _probe(monkeypatch, source_b)
    assert rows, "post-move probe returned no models"
    credential = _probe_home_credential(home)
    assert credential.exists(), (
        "probe home credential dangles at the removed source home, so this and "
        "every later probe answers as a logged-out account"
    )
    assert credential.resolve() == (source_b / "auth.json").resolve(), (
        "probe home credential still names the old source home"
    )
