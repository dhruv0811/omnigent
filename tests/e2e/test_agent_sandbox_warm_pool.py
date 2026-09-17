"""Live warm-pool allocation against an explicitly prepared local demo.

Prepare/start scripts/agent-sandbox-warm-pool-demo.py, then set
OMNIGENT_WARM_POOL_E2E=1 and OMNIGENT_WARM_POOL_DEMO_DIR to that demo's directory.
The real HTTP create path invokes our provider and the native claim controller;
no Kubernetes client, host, runner, or broker is mocked. No LLM turn is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_WARM_POOL_E2E") != "1",
        reason="set OMNIGENT_WARM_POOL_E2E=1 and point OMNIGENT_WARM_POOL_DEMO_DIR at a live demo",
    ),
    pytest.mark.timeout(1080),
]


def test_managed_session_adopts_ready_pod_and_registers_host() -> None:
    """Reuse a pre-request Pod, register the real host/runner, and write its HOME."""
    directory_value = os.environ.get("OMNIGENT_WARM_POOL_DEMO_DIR")
    assert directory_value, "OMNIGENT_WARM_POOL_DEMO_DIR must explicitly select the prepared demo"
    directory = Path(directory_value).expanduser().resolve()
    settings = json.loads((directory / "demo.json").read_text())
    assert Path(settings["checkout"]).resolve() == _ROOT
    config = yaml.safe_load((directory / "server-config.yaml").read_text())
    assert config["sandbox"]["provider"] == "agent_sandbox"
    assert config["sandbox"]["agent_sandbox"]["warm_pool"]
    assert config["sandbox"]["kubernetes"]["kubeconfig"] == str(directory / "kubeconfig")

    verification_path = directory / "verification.json"
    previous = json.loads(verification_path.read_text()) if verification_path.exists() else {}
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "agent-sandbox-warm-pool-demo.py"),
            "verify",
            "--directory",
            str(directory),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=1050,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(verification_path.read_text())
    assert evidence["session_id"] != previous.get("session_id")
    base_url = f"http://127.0.0.1:{settings['port']}"
    session_url = f"{base_url}/v1/sessions/{evidence['session_id']}"
    try:
        before = json.loads((directory / "pods-before.json").read_text())
        assert evidence["pod_uid"] in before
        assert before[evidence["pod_uid"]] == evidence["pod"]
        assert evidence["warm_hit"] and evidence["pool_replenished"]
        assert evidence["claim"] and evidence["sandbox"]

        session = httpx.get(session_url, timeout=10)
        session.raise_for_status()
        assert session.json()["host_id"] == evidence["host_id"]
        assert session.json()["host_online"] is True
        assert session.json()["runner_online"] is True

        marker = subprocess.run(
            [
                "kubectl",
                "--context",
                "kind-omnigent-warm-pool",
                "--kubeconfig",
                str(directory / "kubeconfig"),
                "-n",
                "omnigent-warm-demo",
                "exec",
                evidence["pod"],
                "-c",
                "host",
                "--",
                "cat",
                evidence["workspace_marker"],
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert marker.stdout == "warm pool workspace is writable\n"
    finally:
        response = httpx.delete(session_url, timeout=30)
        response.raise_for_status()
