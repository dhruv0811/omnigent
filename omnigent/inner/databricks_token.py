"""Mint a Databricks bearer via the databricks-sdk unified auth resolver.

Run as ``python3 -m omnigent.inner.databricks_token --profile <p>`` (or
``--host <h>``). This is the auth-command fallback the native-harness gateway
path installs (see
:func:`omnigent.inner.databricks_executor.databricks_bearer_token_command`).

``databricks auth token`` — the CLI mint that command prefers — supports **U2M
only** ("M2M authentication using a client ID and secret is not supported"), so
an agent authenticating as an OAuth **M2M service principal** (a workload
identity on, say, a self-hosted Fargate host) never got a Gateway bearer and
the first request failed. The databricks-sdk's ``Config.authenticate()``
resolves every ``auth_type`` — including ``oauth_service_principal`` (the
client-credentials exchange), env / file OIDC, and a static PAT — so this
delegates to it through
:func:`omnigent.inner.databricks_executor._read_databrickscfg` rather than
reimplementing any OAuth.

Prints the bearer on stdout, or nothing (exit 0) on any failure, so the
caller's shell falls through cleanly — mirroring
:func:`omnigent.host.databricks_credential.main`. A genuine M2M misconfig (bad
secret, unreachable token endpoint) therefore surfaces as an empty bearer / 401
at the gateway rather than a traceback; the harness re-runs this command as
tokens near expiry, so a transient token-endpoint blip self-heals next refresh.
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("--host")
    args, _ = parser.parse_known_args(argv)
    try:
        from omnigent.inner.databricks_executor import _read_databrickscfg

        profile = args.profile or None
        if profile is None:
            # Host-selected mint (no profile): mirror the CLI's
            # ``env -u DATABRICKS_CONFIG_PROFILE databricks auth token --host`` —
            # drop any ambient profile and pin the SDK to this workspace so
            # env / OIDC service-principal credentials mint against it.
            os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
            if args.host:
                os.environ["DATABRICKS_HOST"] = args.host.rstrip("/")
        creds = _read_databrickscfg(profile)
    except Exception:  # noqa: BLE001 - a fallback must never emit noise; fall through.
        return 0
    if creds is None or not creds.token:
        return 0
    sys.stdout.write(creds.token + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
