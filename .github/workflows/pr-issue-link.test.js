// Local unit test for pr-issue-link.js -- mocks the GitHub client and runs the
// real decision logic. No network. Covers the exemption predicates, the
// authoritative per-PR link lookup, dedupe, and that a dry run touches nothing.

const assert = require("assert");
const path = require("path");
const script = require(path.resolve(".github/workflows/pr-issue-link.js"));

// A PR node shaped like the GraphQL search response.
function pr({
  number,
  body = "",
  title = "feat: thing",
  author = "ext",
  assoc = "CONTRIBUTOR",
  bot = false,
  draft = false,
  additions = 100,
  deletions = 0,
  labels = [],
}) {
  return {
    number,
    title,
    isDraft: draft,
    additions,
    deletions,
    authorAssociation: assoc,
    author: { login: author, __typename: bot ? "Bot" : "User" },
    labels: { nodes: labels.map((name) => ({ name })) },
    body,
  };
}

// Run the script over PR nodes. `linked` maps PR number -> closing-issue count.
// `env` overrides process.env for the run.
async function run(nodes, { linked = {}, env = {}, linkError = false, maintainers = [] } = {}) {
  const commented = [];
  const labeled = [];
  let searchCalls = 0;
  const github = {
    repos: {},
    graphql: async (query, vars) => {
      if (query.includes("pullRequest(number:")) {
        if (linkError) throw new Error("boom");
        return {
          repository: {
            pullRequest: {
              closingIssuesReferences: { totalCount: linked[vars.number] ?? 0 },
            },
          },
        };
      }
      const done = searchCalls++ > 0;
      return {
        rateLimit: { remaining: 4999, resetAt: "n/a" },
        search: {
          pageInfo: { hasNextPage: !done, endCursor: "c" },
          nodes: done ? [] : nodes,
        },
      };
    },
    rest: {
      repos: {
        getContent: async () => ({
          data: { content: Buffer.from(maintainers.join("\n"), "utf8").toString("base64") },
        }),
      },
      issues: {
        createLabel: async () => {},
        createComment: async ({ issue_number, body }) => commented.push({ issue_number, body }),
        addLabels: async ({ issue_number, labels: ls }) => labeled.push({ issue_number, labels: ls }),
      },
    },
  };
  const warnings = [];
  const core = { warning: (m) => warnings.push(m), summary: null };
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    await script({ context: { repo: { owner: "o", repo: "r" } }, github, core });
  } finally {
    for (const k of Object.keys(env)) delete process.env[k];
    Object.assign(process.env, saved);
  }
  return { commented, labeled, warnings };
}

const ENFORCE = { ENFORCE: "true" };

// ---- exemption predicates (pure) ----
const { exemptReason } = script;

assert.strictEqual(exemptReason(pr({ number: 1 })), null, "plain unlinked PR is not exempt");
assert.strictEqual(exemptReason(pr({ number: 2, bot: true })), "bot");
assert.strictEqual(exemptReason(pr({ number: 3, draft: true })), "draft");

// Maintainers are exempt via EITHER signal. Both are needed: a maintainer with
// private org membership reads as CONTRIBUTOR, and a maintainer with write
// access may not be listed in .github/MAINTAINER.
for (const assoc of ["MEMBER", "OWNER", "COLLABORATOR"]) {
  assert.strictEqual(
    exemptReason(pr({ number: 30, assoc })),
    "maintainer",
    `${assoc} is exempt by association`
  );
}
assert.strictEqual(
  exemptReason(pr({ number: 31, author: "Maintainer-Person", assoc: "CONTRIBUTOR" }), new Set(["maintainer-person"])),
  "maintainer",
  "MAINTAINER file catches a private-membership maintainer (case-insensitive)"
);
assert.strictEqual(
  exemptReason(pr({ number: 32, author: "outsider" }), new Set(["maintainer-person"])),
  null,
  "a non-maintainer is still enforced"
);
assert.strictEqual(
  exemptReason(pr({ number: 4, labels: ["skip-issue-check"] })),
  "skip-issue-check label"
);
assert.strictEqual(
  exemptReason(pr({ number: 5, additions: 4, deletions: 5 })),
  "trivial",
  "<= 9 changed lines is trivial"
);
assert.strictEqual(
  exemptReason(pr({ number: 6, additions: 6, deletions: 5 })),
  null,
  "10 changed lines is not trivial"
);
assert.strictEqual(exemptReason(pr({ number: 7, title: "Revert \"feat: x\"" })), "revert");
assert.strictEqual(exemptReason(pr({ number: 8, body: "blah\nno-issue\nblah" })), "no-issue opt-out");

// Declared exempt types, matching the real template's checkbox labels.
for (const type of ["Refactor / chore", "Docs", "Test / CI"]) {
  assert.strictEqual(
    exemptReason(pr({ number: 9, body: `## Type of change\n\n- [x] ${type}\n` })),
    "declared chore/docs/test",
    `${type} checked is exempt`
  );
}
// The whole point of the gate: silence must NOT exempt.
assert.strictEqual(
  exemptReason(
    pr({
      number: 10,
      body: "## Type of change\n\n- [ ] Bug fix\n- [ ] Refactor / chore\n- [ ] Docs\n- [ ] Test / CI\n",
    })
  ),
  null,
  "unchecked boxes do not exempt"
);
assert.strictEqual(
  exemptReason(pr({ number: 11, body: "no template at all" })),
  null,
  "a deleted template does not exempt"
);
assert.strictEqual(
  exemptReason(pr({ number: 12, body: "## Type of change\n\n- [x] Bug fix\n- [ ] Docs\n" })),
  null,
  "a declared Bug fix is not exempt"
);

// ---- end-to-end behaviour ----
(async () => {
  // Dry run (the default) must not comment or label.
  {
    const { commented, labeled } = await run([pr({ number: 20 })]);
    assert.strictEqual(commented.length, 0, "dry run must not comment");
    assert.strictEqual(labeled.length, 0, "dry run must not label");
  }

  // Enforcing: an unlinked, non-exempt PR gets one comment + the label.
  {
    const { commented, labeled } = await run([pr({ number: 21, author: "alice" })], { env: ENFORCE });
    assert.strictEqual(commented.length, 1);
    assert.strictEqual(commented[0].issue_number, 21);
    assert.match(commented[0].body, /@alice/);
    assert.match(commented[0].body, /Closes #123/);
    assert.deepStrictEqual(labeled, [{ issue_number: 21, labels: [script.LABEL] }]);
  }

  // A linked PR is left alone even when enforcing.
  {
    const { commented, labeled } = await run([pr({ number: 22 })], {
      linked: { 22: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(commented.length, 0, "linked PR must not be flagged");
    assert.strictEqual(labeled.length, 0);
  }

  // Already-labeled PRs are never commented on twice.
  {
    const { commented } = await run([pr({ number: 23, labels: [script.LABEL] })], { env: ENFORCE });
    assert.strictEqual(commented.length, 0, "label dedupes repeat runs");
  }

  // A failed link lookup must fail closed (skip), never flag.
  {
    const { commented, warnings } = await run([pr({ number: 24 })], {
      env: ENFORCE,
      linkError: true,
    });
    assert.strictEqual(commented.length, 0, "unverifiable PR must not be flagged");
    assert.ok(warnings.some((w) => /Could not resolve links for #24/.test(w)));
  }

  // LIMIT caps how many PRs a single run touches.
  {
    const nodes = [25, 26, 27].map((number) => pr({ number }));
    const { commented } = await run(nodes, { env: { ...ENFORCE, LIMIT: "2" } });
    assert.strictEqual(commented.length, 2, "LIMIT caps flags per run");
  }

  // Maintainer PRs are never commented on, by either signal.
  {
    const { commented } = await run(
      [
        pr({ number: 28, assoc: "MEMBER" }),
        pr({ number: 29, author: "listed-maintainer" }),
        pr({ number: 30, author: "outsider" }),
      ],
      { env: ENFORCE, maintainers: ["listed-maintainer", "# a comment"] }
    );
    assert.deepStrictEqual(
      commented.map((c) => c.issue_number),
      [30],
      "only the non-maintainer is commented on"
    );
  }

  // A missing MAINTAINER file must not crash the run (association still applies).
  {
    const github_err = { env: ENFORCE };
    const { commented, warnings } = await run([pr({ number: 31, assoc: "MEMBER" })], github_err);
    assert.strictEqual(commented.length, 0, "MEMBER stays exempt without the file");
    assert.ok(!warnings.some((w) => /throw/i.test(w)));
  }

  console.log("pr-issue-link.test.js: all assertions passed");
})();
