"""Mint a Databricks bearer via the databricks-sdk unified auth resolver.

Run as ``python3 -m omnigent.inner.databricks_token --host <h> [--profile <p>]``.
This is the auth-command fallback the native-harness gateway path installs (see
:func:`omnigent.inner.databricks_executor.databricks_bearer_token_command`).

``databricks auth token`` supports **U2M only** ("M2M authentication using a
client ID and secret is not supported"), so an agent authenticating as an OAuth
**M2M service principal** (a workload identity, e.g. a self-hosted Fargate host)
never got a Gateway bearer. The databricks-sdk's ``Config.authenticate()`` does
the client-credentials exchange (and env / file OIDC and a static PAT), so this
mints through the SDK rather than reimplementing any OAuth.

Identity is **fail-closed**. A named ``--profile`` is pinned to its own config
section with no ambient (env / OIDC / ``DEFAULT``-section) fallback, so a missing
or broken profile prints nothing rather than silently minting some *other*
identity's token — per-agent attribution is the whole point of M2M. Ambient env
/ OIDC resolution is used only in the unprofiled ``--host`` mode. In both cases
the resolved workspace must match the requested ``--host`` (the gateway's
workspace) before the token is printed, so a profile aimed at a different
workspace can't hand its bearer to this gateway.

Prints the bearer on stdout, or nothing (exit 0) on any failure, so the caller's
shell falls through cleanly — mirroring
:func:`omnigent.host.databricks_credential.main`. A genuine M2M misconfig (bad
secret, unreachable token endpoint) surfaces as an empty bearer / 401 at the
gateway; the harness re-runs this command near expiry, so a transient blip
self-heals next refresh.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlsplit


def _workspace(host: str | None) -> str:
    """Normalized network location of *host* (scheme-insensitive, no trailing slash)."""
    h = (host or "").strip().rstrip("/")
    if not h:
        return ""
    return urlsplit(h if "://" in h else f"https://{h}").netloc.lower()


def _sdk_bearer(profile: str | None, host: str | None) -> tuple[str, str] | None:
    """Return ``(resolved_host, bearer)`` from the databricks-sdk, or ``None``.

    Fail-closed on identity: a named *profile* is pinned to its own section with
    no ambient fallback; only the unprofiled path consults env / OIDC. Raises on
    a genuine SDK error (e.g. an unreachable token endpoint) so :func:`main` can
    fall through quietly.
    """
    from databricks.sdk.config import Config

    if profile:
        # Pinned identity: the profile's own section only. Unlike the executor's
        # _read_databrickscfg, this does NOT retry with ambient credentials on a
        # resolution failure — a broken named profile must not mint as someone else.
        cfg = Config(profile=profile)
    else:
        # Unprofiled: mirror ``databricks auth token --host`` — drop any ambient
        # profile and pin the SDK to this workspace so env / OIDC
        # service-principal credentials mint against it.
        os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
        cfg = Config(host=host) if host else Config()
    auth = cfg.authenticate().get("Authorization", "")
    if not cfg.host or not auth.startswith("Bearer "):
        return None
    return cfg.host, auth[len("Bearer ") :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("--host")
    args, _ = parser.parse_known_args(argv)
    try:
        resolved = _sdk_bearer(args.profile or None, args.host)
    except ImportError:
        # databricks-sdk lives in the `databricks` extra; a base install can't
        # mint M2M. Hint on stderr (stdout stays the token channel) so this is
        # debuggable rather than a silent no-op.
        sys.stderr.write(
            "omnigent.inner.databricks_token: databricks-sdk is not installed; "
            "install the 'databricks' extra to mint M2M / service-principal tokens.\n"
        )
        return 0
    except Exception:  # noqa: BLE001 - a fallback must never emit noise; fall through.
        return 0
    if resolved is None:
        return 0
    resolved_host, bearer = resolved
    # The gateway base URL is pinned to ``--host``; only print a token minted for
    # that same workspace, so a profile (or ambient creds) aimed elsewhere can't
    # present its bearer to this gateway.
    if args.host and _workspace(resolved_host) != _workspace(args.host):
        return 0
    if not bearer:
        return 0
    sys.stdout.write(bearer + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
