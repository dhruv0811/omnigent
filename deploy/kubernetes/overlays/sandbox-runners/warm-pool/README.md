# Native agent-sandbox warm pools

Warm pools prepare spare Pods before a session needs them. Omnigent claims a
compatible Sandbox, delivers its managed-host identity to the waiting bootstrap,
and starts repository preparation and the normal host. GitHub and Databricks
credentials still come from the existing owner-bound broker and credential store.

This is opt-in for `provider: agent_sandbox`. The existing direct Sandbox path
continues to support deployments without a pool and requests whose trusted
agent classifier differs from the pool's classifier. Other profile mismatches
fail explicitly so an incompatible template cannot serve the session.

## Configure a deployment

Install the upstream v1.0.0 controller with extensions in your chosen cluster:

```bash
kubectl --context YOUR_CONTEXT apply --server-side -f \
  https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.0/sandbox-with-extensions.yaml
```

Build the host image from the same Omnigent revision as the server; it must
contain the warm bootstrap module. Set the image and the other desired settings
in your server config, then add the pool reference:

```yaml
sandbox:
  provider: agent_sandbox
  server_url: http://omnigent.omnigent.svc.cluster.local:8000
  agent_sandbox:
    warm_pool: omnigent-default-v1
  kubernetes:
    namespace: omnigent-sandboxes
    service_account: omnigent-runner
    image: your-registry/omnigent-host:your-version
```

Generate the template and pool from that exact config. Use the same workspace
volume environment settings for generation and for the server:

```bash
export OMNIGENT_AGENT_SANDBOX_WORKSPACE_SIZE=5Gi
python -m omnigent.onboarding.sandboxes.agent_sandbox_warm_pool \
  --config server-config.yaml --name omnigent-default-v1 --replicas 2 \
  > warm-pool.yaml
kubectl --context YOUR_CONTEXT apply -f warm-pool.yaml
kubectl --context YOUR_CONTEXT apply -k deploy/kubernetes/overlays/sandbox-runners/warm-pool
```

Apply this additive RBAC overlay alongside the existing sandbox-runners
deployment. It grants the server ServiceAccount claim create/get/patch/delete,
template/pool get, and `pods/exec` create/get in `omnigent-sandboxes`. The
application does not create or modify shared pools. Kubernetes RBAC cannot
restrict exec to a particular command; enabling warm mode expands the server's
permissions within the runner namespace. Direct mode does not need this overlay.

New claims carry a temporary deletion deadline while allocation is in progress.
After registering the owner-bound host, Omnigent clears that claim deadline and
uses the Sandbox inactivity deadline. The claim patch permission supports this
handoff and prevents abandoned launches from retaining allocated resources.

Pool profiles include the image, resources, mounts, scheduling and security
settings, and agent classifier. For a pool dedicated to a built-in agent, pass
`--agent-name` with its exact name when generating the template. A generic pool
is unclassified and works with uploaded, session-scoped agents. Admission-time
credential injection must already match the pool's profile at Pod creation.
Use a new versioned pool name when changing profiles so old spare Pods do not
silently continue serving the previous configuration.

### Initial-release constraint: allocated profiles stay fixed

An allocated warm Sandbox retains its original static profile. Changing the
server's image, mounts, resources, or expected agent classifier can prevent that
existing session from waking. Switching an agent can also remove its built-in
classification and trigger this mismatch on the next wake. The wake fails with
a profile error and preserves the Sandbox/PVC; it does not change an existing
admission identity to make the new session configuration fit.

Changing `sandbox.agent_sandbox.warm_pool` selects a pool for new allocations
only. It does not migrate existing claims or their workspaces. The generator's
`Recreate` strategy replaces unused pool inventory only. Use direct provisioning
for new sessions that need infrastructure or classifier changes across wake;
disabling pooling does not convert existing warm allocations into direct ones.

The bootstrap and host are both regular containers, and each reserves the
configured resources. Kubernetes sums their requests: `250m` CPU and `384Mi`
memory in the config means `500m` and `768Mi` per pooled Pod, before injected
sidecars. Account for both spare and allocated Pods when sizing node capacity.

Generated templates set `networkPolicyManagement: Unmanaged` and use cluster
DNS, preserving the existing direct provider's networking model. Operators must
provide the production NetworkPolicies, including DNS and callback access to
Omnigent and any private Git/Databricks endpoints. The template does not install
a default isolation policy.

Do not add environment variables or PVC overrides to individual claims: upstream
creates a cold Sandbox for those overrides. Session-specific launch data arrives
after allocation. Each template-created HOME PVC belongs to its Sandbox and
remains with it through suspension. Inactivity deadlines belong on the Sandbox;
claim lifecycle expiry deletes backing resources and must not be used for idle
suspension. Allocated Sandboxes are not returned to the spare pool.

## Local Colima demo

The helper creates a separate kind cluster named `omnigent-warm-pool`, a
dedicated kubeconfig, and local application state under
`~/.omnigent-warm-pool-demo`. It uses port 8101, one spare Pod, and arm64 host
images. It does not stop other servers, change your default Kubernetes context,
or reuse credentials from existing labs. The new cluster shares Colima's CPU,
memory, and disk with other clusters.

Prerequisites: running Colima, Docker, kind, kubectl, and this checkout's Python
environment with the `kubernetes` extra. Start Colima if needed, then run from
the repository root:

```bash
colima start --cpu 4 --memory 6 --disk 40
uv sync --extra kubernetes --group dev
python3 scripts/agent-sandbox-warm-pool-demo.py prepare
python3 scripts/agent-sandbox-warm-pool-demo.py start
python3 scripts/agent-sandbox-warm-pool-demo.py verify
python3 scripts/agent-sandbox-warm-pool-demo.py status
```

`prepare` builds this checkout's host image, loads it into kind using a
single-platform image archive, installs the pinned upstream controller, and
generates the matching pool. Image build/pull time occurs here, before measuring
session startup. It can take several minutes. To retry preparation after a
successful image build, add `--skip-build`.

To use a host image you already built locally, select its explicit tag during
the first preparation. This skips the build and loads that image into kind:

```bash
python3 scripts/agent-sandbox-warm-pool-demo.py prepare \
  --skip-build --image omnigent-host:warm-pool-demo
```

`--image` requires an existing local image and `--skip-build`; the helper does
not pull it or silently replace a previously prepared demo's image selection.

The server binds to Mac loopback. Pods reach it through Colima's
`host.docker.internal` gateway. The default is an API-only server. To include the
web UI, pass an existing built bundle directory during the first preparation:

```bash
python3 scripts/agent-sandbox-warm-pool-demo.py prepare \
  --web-ui-dist /absolute/path/to/omnigent/server/static/web-ui
```

`--python /absolute/path/to/.venv/bin/python` can reuse another checkout's
environment. The helper always starts the server with this checkout as its
working directory. `--directory` and `--port` select alternative local state and
server ports; the cluster name is fixed to protect unrelated clusters, so run
one instance at a time. Preparation options are saved in `demo.json`.

`verify` uploads a session-scoped Claude SDK agent without an initial message.
It needs no model credentials and checks:

1. Pod UIDs are recorded **before** session creation in `pods-before.json`.
2. The managed host and runner register online through the normal tunnel.
3. A new claim identifies the allocated Sandbox and its actual Pod.
4. That Pod's UID matches one recorded before the request.
5. The host can write and read `/home/omnigent/workspace/WARM-POOL-MARKER`.
6. The pool replenishes to one spare member.

`verification.json` records the session, host, claim, Sandbox, Pod UID, and
elapsed time until host and runner registration. It measures warm infrastructure
activation, not time to a model response. The helper leaves the session and
workspace available for inspection. Default kind networking does not enforce
NetworkPolicy, so this demo does not establish production network isolation.

The same request-to-host journey is available as an opt-in live E2E test. Prepare
and start the demo first, then run:

```bash
OMNIGENT_WARM_POOL_E2E=1 \
  OMNIGENT_WARM_POOL_DEMO_DIR="$HOME/.omnigent-warm-pool-demo" \
  .venv/bin/python -m pytest tests/e2e/test_agent_sandbox_warm_pool.py -v
```

This test calls the real configured provider and native controllers; it does not
mock Kubernetes or require an LLM key. It deletes its session after checking the
allocated Pod and workspace. Without the explicit opt-in flag it skips.

Inspect the results with the dedicated kubeconfig:

```bash
kubectl --context kind-omnigent-warm-pool \
  --kubeconfig "$HOME/.omnigent-warm-pool-demo/kubeconfig" \
  -n omnigent-warm-demo get sandboxclaim,sandbox,pod,pvc
cat "$HOME/.omnigent-warm-pool-demo/verification.json"
```

To check durable wake without model credentials, leave the verified session
untouched and run:

```bash
python3 scripts/agent-sandbox-warm-pool-demo.py verify-wake
```

This command validates the saved allocation UIDs, expires only that Sandbox with
`Retain`, and waits for its Pod and host/runner connections to disappear. It then
uses the public `retry_session` control event to invoke the real managed resume
path without submitting a prompt. The same host, Sandbox UID, and HOME PVC UID
must return with a new Pod UID and the original workspace marker. Results go to
`wake-verification.json`.

The check requires the empty, idle session created by `verify`; it refuses a
session with conversation items so it cannot recover an old prompt. Run `verify`
again before repeating `verify-wake`, or after the E2E test deleted its session.
Normal UI sessions also resume when a new message arrives, after model access
has been configured.

## Check Connected Accounts

Configure the existing credential store and GitHub/Databricks connection
providers for the isolated server. The helper intentionally does not inherit
provider credentials from the invoking agent session. Server-only environment
settings can be supplied as a JSON object in
`~/.omnigent-warm-pool-demo/server-env.json`; keep that file private and outside
the repository. Configuration, launch tokens, and provider tokens should never
be copied into screenshots or terminal output. Restart the demo server after
changing its environment.

Use OAuth redirect URLs matching this demo's address and port. If an existing
OAuth app only allows another lab's port, register a separate redirect before
testing; the helper leaves that existing server running.

With the UI available at `http://127.0.0.1:8101`:

1. Connect a test GitHub account and a test Databricks workspace in Connected
   Accounts. Keep the pool free of static provider credential Secrets, so they
   cannot mask broker failures.
2. Create a pooled session with a private test repository, then use its terminal
   to run `git fetch` and `gh api user --jq .login`. Confirm the expected owner.
   Repeat with two repository workspaces to check clone preparation.
3. Run `databricks current-user me` in the allocated host, then a small model
   request and a read-only Databricks MCP request through your chosen harness.
   Confirm that the account/workspace matches the connected owner.
4. Suspend and wake the same session. Verify the workspace marker and repository
   files survive, and both services still authenticate after activation refresh.
5. Disconnect one service and check that subsequent broker requests stop
   authenticating through that connection. Delete the session and verify its
   claim, Sandbox, Pod, and owned PVC are removed while the pool stays stocked.

For built-in agents selected in the UI, generate a pool with the matching
`--agent-name`; otherwise profile mismatch can select direct provisioning and
invalidate a claimed warm-hit result. Check the claim and pre-request Pod UID
for every timing measurement. Two-user account separation requires a deployment
with real user authentication; this single-user loopback demo cannot establish
that property.

Stop the local server or remove only the demo cluster:

```bash
python3 scripts/agent-sandbox-warm-pool-demo.py stop
python3 scripts/agent-sandbox-warm-pool-demo.py cleanup
```

Cleanup preserves config, logs, and verification evidence in the demo directory,
and leaves the built Docker image cached. Deleting the demo cluster permanently
deletes its workspaces.
