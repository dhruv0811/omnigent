#!/usr/bin/env python3
"""Run an isolated kind/Colima demo of native agent-sandbox warm allocation."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = "omnigent-warm-pool"
NAMESPACE = "omnigent-warm-demo"
POOL = "omnigent-warm-demo-v1"
RELEASE = "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.0"


def run(*args: str, capture: bool = False, **kwargs: Any) -> str:
    result = subprocess.run(
        args, check=True, text=True, capture_output=capture, cwd=ROOT, **kwargs
    )
    return result.stdout if capture else ""


class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.directory = args.directory.expanduser().resolve()
        self.settings_path = self.directory / "demo.json"
        self.settings: dict[str, Any] = {}
        if self.settings_path.exists():
            self.settings = json.loads(self.settings_path.read_text())
            if self.settings["checkout"] != str(ROOT):
                raise RuntimeError(f"Demo belongs to checkout {self.settings['checkout']}")
        self.kubeconfig = self.directory / "kubeconfig"
        self.config = self.directory / "server-config.yaml"

    def kubectl(self, *args: str, capture: bool = False, **kwargs: Any) -> str:
        return run(
            "kubectl",
            "--context",
            f"kind-{CLUSTER}",
            "--kubeconfig",
            str(self.kubeconfig),
            "-n",
            NAMESPACE,
            *args,
            capture=capture,
            **kwargs,
        )

    def resources(self, kind: str) -> list[dict[str, Any]]:
        return json.loads(self.kubectl("get", kind, "-o", "json", capture=True))["items"]

    def resource(self, kind: str, name: str) -> dict[str, Any]:
        return json.loads(self.kubectl("get", kind, name, "-o", "json", capture=True))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.settings['port']}"

    def request(
        self,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
        timeout: float = 10,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url + path, data=data, headers={"Content-Type": content_type}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def environment(self) -> dict[str, str]:
        # Explicit inheritance keeps an enclosing agent session out of the demo.
        names = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
        env = {key: os.environ[key] for key in names if key in os.environ}
        extra = self.directory / "server-env.json"
        if extra.exists():
            values = json.loads(extra.read_text())
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise RuntimeError("server-env.json must contain string names and values")
            if any(key in {"HOME", "CODEX_HOME", "PYTHONPATH"} for key in values):
                raise RuntimeError(
                    "server-env.json may not override HOME, CODEX_HOME, or PYTHONPATH"
                )
            env.update(values)
        env.update(
            {
                "OMNIGENT_CONFIG_HOME": str(self.directory / "config"),
                "OMNIGENT_DATA_DIR": str(self.directory / "data"),
                "OMNIGENT_DATABASE_URI": f"sqlite:///{self.directory / 'data' / 'chat.db'}",
                "OMNIGENT_TELEMETRY_DISABLED": "1",
                "OMNIGENT_AGENT_SANDBOX_WORKSPACE_SIZE": "1Gi",
            }
        )
        if self.settings.get("web_ui_dist"):
            env["OMNIGENT_WEB_UI_DIST"] = self.settings["web_ui_dist"]
        else:
            env["OMNIGENT_SKIP_WEB_UI"] = "true"
        return env

    def prepare(self) -> None:
        for tool in ("docker", "kind", "kubectl"):
            if not shutil.which(tool):
                raise RuntimeError(f"Install {tool} before preparing the demo")
        run("docker", "info", capture=True)
        if self.args.image:
            if not self.args.skip_build:
                raise RuntimeError("Use --image with --skip-build for an existing local image")
            run("docker", "image", "inspect", self.args.image, capture=True)
            if self.settings and self.settings["image"] != self.args.image:
                raise RuntimeError(
                    "This demo already uses another image; choose a fresh directory"
                )
        if not self.settings:
            clusters = run("kind", "get", "clusters", capture=True).splitlines()
            if CLUSTER in clusters:
                raise RuntimeError(f"Cluster {CLUSTER} already exists outside this demo directory")
            python = self.args.python.expanduser().absolute()
            if not python.is_file():
                raise RuntimeError("Create .venv with the kubernetes extra, or pass --python")
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.settings = {
                "checkout": str(ROOT),
                "cluster": CLUSTER,
                "port": self.args.port,
                "python": str(python),
                "image": self.args.image or f"omnigent-host:warm-demo-{uuid.uuid4().hex[:10]}",
                "web_ui_dist": str(self.args.web_ui_dist.expanduser().resolve())
                if self.args.web_ui_dist
                else None,
            }
            self.settings_path.write_text(json.dumps(self.settings, indent=2) + "\n")
        for name in ("config", "data", "artifacts"):
            (self.directory / name).mkdir(exist_ok=True)
        if not self.config.exists():
            config = {
                "sandbox": {
                    "provider": "agent_sandbox",
                    "server_url": f"http://host.docker.internal:{self.settings['port']}",
                    "agent_sandbox": {"warm_pool": POOL},
                    "kubernetes": {
                        "namespace": NAMESPACE,
                        "service_account": "omnigent-runner",
                        "image": self.settings["image"],
                        "in_cluster": False,
                        "kubeconfig": str(self.kubeconfig),
                        "secret_name": None,
                        "node_selector": {"kubernetes.io/arch": "arm64"},
                        "pod_ready_timeout_s": 300,
                        "resources": {
                            "requests": {"cpu": "250m", "memory": "384Mi"},
                            "limits": {"cpu": "2", "memory": "2Gi"},
                        },
                    },
                }
            }
            self.config.write_text(json.dumps(config, indent=2) + "\n")
        if not self.args.skip_build:
            print("Building the current checkout's arm64 host image...", flush=True)
            run(
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "--target",
                "host",
                "-f",
                "deploy/docker/Dockerfile",
                "-t",
                self.settings["image"],
                ".",
            )
        clusters = run("kind", "get", "clusters", capture=True).splitlines()
        if CLUSTER not in clusters:
            run(
                "kind",
                "create",
                "cluster",
                "--name",
                CLUSTER,
                "--image",
                self.args.node_image,
                "--kubeconfig",
                str(self.kubeconfig),
            )
        elif not self.kubeconfig.exists():
            raise RuntimeError("Owned cluster exists but its dedicated kubeconfig is missing")
        image_archive = self.directory / "host-image.tar"
        run(
            "docker",
            "save",
            "--platform",
            "linux/arm64",
            self.settings["image"],
            "-o",
            str(image_archive),
        )
        try:
            run("kind", "load", "image-archive", "--name", CLUSTER, str(image_archive))
        finally:
            image_archive.unlink(missing_ok=True)
        self.kubectl("apply", "--server-side", "-f", f"{RELEASE}/sandbox-with-extensions.yaml")
        self.kubectl(
            "-n",
            "agent-sandbox-system",
            "rollout",
            "status",
            "deployment/agent-sandbox-controller",
            "--timeout=180s",
        )
        resources = {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "omnigent-runner", "namespace": NAMESPACE},
                    "automountServiceAccountToken": False,
                },
            ],
        }
        self.kubectl("apply", "-f", "-", input=json.dumps(resources))
        manifest = run(
            self.settings["python"],
            "-m",
            "omnigent.onboarding.sandboxes.agent_sandbox_warm_pool",
            "--config",
            str(self.config),
            "--name",
            POOL,
            "--replicas",
            "1",
            capture=True,
            env=self.environment(),
        )
        (self.directory / "warm-pool.yaml").write_text(manifest)
        self.kubectl("apply", "-f", str(self.directory / "warm-pool.yaml"))
        self.wait_for_pool()
        print(f"Prepared {self.directory}. Run this helper with start, then verify.")

    def wait_for_pool(self) -> None:
        self.kubectl(
            "wait",
            f"sandboxwarmpool/{POOL}",
            "--for=jsonpath={.status.readyReplicas}=1",
            "--timeout=300s",
        )

    def start(self) -> None:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", self.settings["port"]))
            except OSError as exc:
                raise RuntimeError(f"Port {self.settings['port']} is already in use") from exc
        log = self.directory / "server.log"
        with log.open("a") as output:
            process = subprocess.Popen(
                [
                    self.settings["python"],
                    "-m",
                    "omnigent.cli",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.settings["port"]),
                    "--artifact-location",
                    str(self.directory / "artifacts"),
                    "-c",
                    str(self.config),
                    "--no-open",
                ],
                cwd=ROOT,
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (self.directory / "server.pid").write_text(str(process.pid))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Server exited; inspect {log}")
            try:
                self.request("/health")
                print(f"Server ready: {self.url}. Log: {log}")
                return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        raise RuntimeError(f"Server did not become healthy; inspect {log}")

    def status(self) -> None:
        try:
            self.request("/health")
            print(f"Server: {self.url} (healthy)")
        except (urllib.error.URLError, TimeoutError):
            print(f"Server: {self.url} (not responding)")
        self.kubectl("get", "sandboxwarmpool,sandboxclaim,sandbox,pod,pvc")

    def create_session(self) -> str:
        spec = (
            b"spec_version: 1\nname: warm-pool-demo\n"
            b"executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
            b"prompt: Help the user inspect their sandbox workspace.\n"
        )
        bundle = io.BytesIO()
        with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
            entry = tarfile.TarInfo("config.yaml")
            entry.size = len(spec)
            archive.addfile(entry, io.BytesIO(spec))
        boundary = uuid.uuid4().hex
        metadata = json.dumps({"title": "Warm pool demo", "host_type": "managed"})
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
                f"Content-Type: application/json\r\n\r\n{metadata}\r\n"
                f'--{boundary}\r\nContent-Disposition: form-data; name="bundle"; '
                'filename="agent.tar.gz"\r\nContent-Type: application/gzip\r\n\r\n'
            ).encode()
            + bundle.getvalue()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        result = self.request(
            "/v1/sessions", data=body, content_type=f"multipart/form-data; boundary={boundary}"
        )
        return result["session_id"]

    def verify(self) -> None:
        self.wait_for_pool()
        before = {
            pod["metadata"]["uid"]: pod["metadata"]["name"] for pod in self.resources("pods")
        }
        previous_claims = {item["metadata"]["uid"] for item in self.resources("sandboxclaims")}
        (self.directory / "pods-before.json").write_text(json.dumps(before, indent=2) + "\n")
        print(f"Recorded {len(before)} pre-request Pod UIDs", flush=True)
        started = time.monotonic()
        session_id = self.create_session()
        print(f"Session: {session_id}", flush=True)
        deadline = started + 420
        while time.monotonic() < deadline:
            session = self.request(f"/v1/sessions/{session_id}")
            status = session.get("sandbox_status") or {}
            if status.get("stage") == "failed":
                raise RuntimeError(
                    f"Managed launch failed; inspect session {session_id} and server.log"
                )
            if session.get("host_online") and session.get("runner_online"):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"Host/runner did not register; inspect session {session_id}")
        online_seconds = round(time.monotonic() - started, 2)
        claims = [
            item
            for item in self.resources("sandboxclaims")
            if item["metadata"]["uid"] not in previous_claims
        ]
        if len(claims) != 1:
            raise RuntimeError(f"Expected one new claim, observed {len(claims)}")
        claim = claims[0]
        sandbox_name = claim["status"]["sandbox"]["name"]
        sandbox = self.resource("sandbox", sandbox_name)
        pod_name = (
            sandbox["metadata"]
            .get("annotations", {})
            .get("agents.x-k8s.io/pod-name", sandbox_name)
        )
        pod = self.resource("pod", pod_name)
        pod_uid = pod["metadata"]["uid"]
        if pod_uid not in before:
            raise RuntimeError(
                "Allocated Pod UID did not exist before the request: cold allocation"
            )
        self.kubectl(
            "exec",
            pod_name,
            "-c",
            "host",
            "--",
            "python",
            "-c",
            "from pathlib import Path; "
            "p = Path('/home/omnigent/workspace/WARM-POOL-MARKER'); "
            "p.write_text('warm pool workspace is writable\\n'); "
            "assert p.read_text() == 'warm pool workspace is writable\\n'",
        )
        self.wait_for_pool()
        result = {
            "session_id": session_id,
            "host_id": session["host_id"],
            "claim": claim["metadata"]["name"],
            "claim_uid": claim["metadata"]["uid"],
            "sandbox": sandbox_name,
            "sandbox_uid": sandbox["metadata"]["uid"],
            "pod": pod_name,
            "pod_uid": pod_uid,
            "warm_hit": True,
            "host_and_runner_online_seconds": online_seconds,
            "workspace_marker": "/home/omnigent/workspace/WARM-POOL-MARKER",
            "pool_replenished": True,
        }
        (self.directory / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        print("Host registration, warm allocation, workspace write, and replenishment passed.")
        print("No model inference was requested; check Connected Accounts separately.")

    def verify_wake(self) -> None:
        evidence = json.loads((self.directory / "verification.json").read_text())
        if not evidence.get("claim_uid") or not evidence.get("sandbox_uid"):
            raise RuntimeError("Run verify again to record allocation UIDs before testing wake")
        session_id = evidence["session_id"]
        session_path = f"/v1/sessions/{session_id}"
        session = self.request(session_path)
        if (
            session.get("host_id") != evidence["host_id"]
            or session.get("status") != "idle"
            or session.get("items")
            or session.get("harness") != "claude-sdk"
        ):
            raise RuntimeError("Wake verification requires the untouched, idle demo session")
        claim = self.resource("sandboxclaim", evidence["claim"])
        sandbox = self.resource("sandbox", evidence["sandbox"])
        pod = self.resource("pod", evidence["pod"])
        if (
            claim["metadata"]["uid"] != evidence["claim_uid"]
            or claim.get("status", {}).get("sandbox", {}).get("name") != evidence["sandbox"]
            or sandbox["metadata"]["uid"] != evidence["sandbox_uid"]
            or pod["metadata"]["uid"] != evidence["pod_uid"]
        ):
            raise RuntimeError("The verified allocation changed; run verify for a fresh session")
        home = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "home")
        if "persistentVolumeClaim" not in home:
            raise RuntimeError("Wake verification requires a durable HOME PVC")
        pvc_name = home["persistentVolumeClaim"]["claimName"]
        pvc_uid = self.resource("pvc", pvc_name)["metadata"]["uid"]
        marker = self.kubectl(
            "exec",
            evidence["pod"],
            "-c",
            "host",
            "--",
            "cat",
            evidence["workspace_marker"],
            capture=True,
        )
        if marker != "warm pool workspace is writable\n":
            raise RuntimeError("The verified workspace marker changed")
        print(f"Suspending verified Sandbox {evidence['sandbox']}", flush=True)
        self.kubectl(
            "patch",
            "sandbox",
            evidence["sandbox"],
            "--type=merge",
            "--patch-file",
            "-",
            input=json.dumps(
                {
                    "metadata": {
                        "uid": evidence["sandbox_uid"],
                        "resourceVersion": sandbox["metadata"]["resourceVersion"],
                    },
                    "spec": {"shutdownPolicy": "Retain", "shutdownTime": "2000-01-01T00:00:00Z"},
                }
            ),
        )
        self.kubectl("wait", f"pod/{evidence['pod']}", "--for=delete", "--timeout=180s")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            session = self.request(session_path)
            if not session.get("host_online") and not session.get("runner_online"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Deleted Pod's host/runner still appear online; inspect server.log")
        started = time.monotonic()
        recovery = self.request(
            f"{session_path}/events",
            data=json.dumps({"type": "retry_session", "data": {}}).encode(),
            timeout=420,
        )
        deadline = started + 420
        while time.monotonic() < deadline:
            session = self.request(session_path)
            if session.get("host_online") and session.get("runner_online"):
                break
            if (session.get("sandbox_status") or {}).get("stage") == "failed":
                raise RuntimeError("Managed wake failed; inspect the session and server.log")
            time.sleep(1)
        else:
            raise RuntimeError("Managed host/runner did not register after wake")
        resumed_sandbox = self.resource("sandbox", evidence["sandbox"])
        resumed_pod_name = (
            resumed_sandbox["metadata"]
            .get("annotations", {})
            .get("agents.x-k8s.io/pod-name", evidence["sandbox"])
        )
        resumed_pod = self.resource("pod", resumed_pod_name)
        if (
            session.get("host_id") != evidence["host_id"]
            or resumed_sandbox["metadata"]["uid"] != evidence["sandbox_uid"]
            or self.resource("sandboxclaim", evidence["claim"])["metadata"]["uid"]
            != evidence["claim_uid"]
            or self.resource("pvc", pvc_name)["metadata"]["uid"] != pvc_uid
            or resumed_pod["metadata"]["uid"] == evidence["pod_uid"]
        ):
            raise RuntimeError("Wake did not preserve the expected host, allocation, and PVC")
        resumed_home = next(
            volume for volume in resumed_pod["spec"]["volumes"] if volume["name"] == "home"
        )
        if resumed_home.get("persistentVolumeClaim", {}).get("claimName") != pvc_name:
            raise RuntimeError("Resumed Pod mounts a different HOME PVC")
        restored_marker = self.kubectl(
            "exec",
            resumed_pod_name,
            "-c",
            "host",
            "--",
            "cat",
            evidence["workspace_marker"],
            capture=True,
        )
        if restored_marker != marker:
            raise RuntimeError("Workspace marker did not survive suspension")
        result = {
            "session_id": session_id,
            "host_id": evidence["host_id"],
            "claim": evidence["claim"],
            "sandbox": evidence["sandbox"],
            "sandbox_uid": evidence["sandbox_uid"],
            "pvc": pvc_name,
            "pvc_uid": pvc_uid,
            "old_pod_uid": evidence["pod_uid"],
            "new_pod_uid": resumed_pod["metadata"]["uid"],
            "marker_preserved": True,
            "recovery": recovery.get("recovery"),
            "host_and_runner_online_seconds": round(time.monotonic() - started, 2),
        }
        (self.directory / "wake-verification.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        print("Durable suspend/wake passed without submitting a model prompt.")

    def stop(self) -> None:
        pid_path = self.directory / "server.pid"
        if not pid_path.exists():
            print("No recorded demo server PID")
            return
        pid = int(pid_path.read_text())
        try:
            command = run("ps", "-p", str(pid), "-o", "command=", capture=True)
        except subprocess.CalledProcessError:
            pid_path.unlink()
            return
        if "omnigent.cli server" not in command or str(self.config) not in command:
            raise RuntimeError(
                "Recorded PID no longer belongs to this demo; refusing to signal it"
            )
        os.kill(pid, signal.SIGTERM)
        pid_path.unlink()
        print(f"Stopped demo server {pid}")

    def cleanup(self) -> None:
        self.stop()
        run("kind", "delete", "cluster", "--name", CLUSTER, "--kubeconfig", str(self.kubeconfig))
        print(
            f"Removed demo cluster. Preserved local config, logs, and evidence in {self.directory}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare", "start", "status", "verify", "verify-wake", "stop", "cleanup"),
    )
    parser.add_argument("--directory", type=Path, default=Path.home() / ".omnigent-warm-pool-demo")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv" / "bin" / "python")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--web-ui-dist", type=Path)
    parser.add_argument("--node-image", default="kindest/node:v1.34.0")
    parser.add_argument("--image", help="Existing local host image tag; requires --skip-build")
    parser.add_argument(
        "--skip-build", action="store_true", help="Reuse this demo's existing image"
    )
    args = parser.parse_args()
    try:
        demo = Demo(args)
        if args.command != "prepare" and not demo.settings:
            raise RuntimeError("Run prepare first")
        getattr(demo, args.command.replace("-", "_"))()
    except (RuntimeError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        parser.exit(1, f"Demo failed: {exc}\n")


if __name__ == "__main__":
    main()
