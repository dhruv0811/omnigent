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

## Verify a deployment

The opt-in live E2E test exercises the configured provider and native controllers.
Use a test server that accepts API requests without an authentication header,
such as a single-user development server. Configure it with a generic,
unclassified pool and wait for at least one ready spare. Its namespace and pool
must be isolated from other test launchers so the test can identify its claim.
The host image must support the Claude SDK harness used by the test bundle.

Set the server address and Kubernetes connection explicitly:

```bash
OMNIGENT_WARM_POOL_E2E=1 \
  OMNIGENT_WARM_POOL_SERVER_URL=http://127.0.0.1:8000 \
  OMNIGENT_WARM_POOL_KUBECONFIG=/absolute/path/to/test-kubeconfig \
  OMNIGENT_WARM_POOL_CONTEXT=YOUR_TEST_CONTEXT \
  OMNIGENT_WARM_POOL_NAMESPACE=omnigent-sandboxes \
  OMNIGENT_WARM_POOL_NAME=omnigent-default-v1 \
  .venv/bin/python -m pytest tests/e2e/test_agent_sandbox_warm_pool.py -v
```

The test records Pod UIDs before creating an empty managed session. It verifies
that the allocated Pod already existed, waits for host and runner registration,
writes and reads a workspace marker, and checks that spare capacity replenishes.
It deletes its session afterward. No model request or connected-account
credentials are required; without the explicit opt-in flag, the test skips.

Validate credential and lifecycle behavior separately in an authenticated test
deployment. Connect test GitHub and Databricks accounts, create a pooled session
with a private repository, and check `git fetch`, `gh api user --jq .login`, and
`databricks current-user me`. Keep static provider credential Secrets out of the
pool during broker validation so they cannot hide a failed connection lookup.
Verify that the same workspace survives suspension and wake, and that both
services still authenticate after activation refresh. Check account separation
with two authenticated users and verify that disconnecting a service stops new
broker requests from authenticating through that connection.

For a built-in agent, generate a pool with its matching `--agent-name`; otherwise
classifier fallback can select direct provisioning. Confirm the claim and
pre-request Pod UID for every timing measurement. A successful allocation alone
does not distinguish a warm hit from upstream cold creation. Measure image
preparation separately from workspace preparation, broker setup, host/runner
registration, and the first model response.
