// Scan PRs opened in the last 24 hours and flag any that don't link an issue.
// Runs hourly from the demo-check sweep; the 24-hour window ensures every new PR
// is checked even if it was opened just before a cron tick. Flagged PRs get one
// comment plus the `missing-issue-link` label, which also dedupes subsequent
// runs. Nothing is ever closed -- the label is the signal a future merge gate or
// closer can read (Prow's model: plugins label, only the merge queue blocks).
//
// ENFORCE=false (the default) is a dry run: it resolves every verdict and writes
// them to the step summary without commenting or labeling.
//
// Exemptions, in the order applied:
//   - bots (release automation can't file issues; our CI bots author as
//     CONTRIBUTOR, not MEMBER, so association checks miss them)
//   - drafts
//   - an affirmatively checked `Refactor / chore`, `Docs`, or `Test / CI` box.
//     Note this requires a DECLARATION: an empty or deleted template does NOT
//     exempt, or removing the template would become the way to skip the rule.
//   - trivial changes (<= 9 changed lines, the same threshold pr-size.js uses
//     for size/XS) -- Spark's "trivial changes ... do not require a JIRA"
//   - reverts
//   - `no-issue` on a line of its own in the body (an escape hatch a first-time
//     contributor can use; they cannot apply labels)
//   - `skip-issue-check` label (maintainer override)
//   - maintainers, by authorAssociation OR the .github/MAINTAINER file. Both are
//     needed: a maintainer whose org membership is private reads as CONTRIBUTOR,
//     and a maintainer may hold write access without being listed in the file.

const MS_PER_HOUR = 60 * 60 * 1000;
const HOURS_TO_SCAN = 24;
const LABEL = "missing-issue-link";
const OVERRIDE_LABEL = "skip-issue-check";
// Same threshold pr-size.js uses for size/XS.
const TRIVIAL_LINES = 9;

const MAINTAINER_ASSOCIATIONS = ["MEMBER", "OWNER", "COLLABORATOR"];

// Change types that describe work with no user-visible behaviour, and so no
// tracking issue. Must match the "Type of change" boxes in
// .github/pull_request_template.md.
const DECLARED_EXEMPT_TYPE = /- \[[xX]\]\s*(?:Refactor \/ chore|Docs|Test \/ CI)\b/;
// `no-issue` alone on a line (mirrors Prow's `releasenote: none` escape hatch).
const OPT_OUT = /^[ \t>]*no-issue[ \t]*$/im;

const QUERY = `
  query($cursor: String, $searchQuery: String!) {
    rateLimit { remaining resetAt }
    search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
          number
          title
          isDraft
          additions
          deletions
          authorAssociation
          author { login __typename }
          labels(first: 30) { nodes { name } }
          body
        }
      }
    }
  }
`;

// Resolved per PR rather than in the batch search above: the search connection
// under-reports closingIssuesReferences, and a false "unlinked" verdict is the
// one mistake that reaches a contributor.
const LINK_QUERY = `
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        closingIssuesReferences(first: 1) { totalCount }
      }
    }
  }
`;

function isBot(pr) {
  const author = pr.author || {};
  return author.__typename === "Bot" || (author.login || "").endsWith("[bot]");
}

// Returns the reason this PR is exempt, or null when the rule applies.
// `maintainers` is the lowercased login set from .github/MAINTAINER.
// Order matters only for which reason gets reported.
function exemptReason(pr, maintainers = new Set()) {
  const body = pr.body ?? "";
  const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
  if (isBot(pr)) return "bot";
  if (pr.isDraft) return "draft";
  if (MAINTAINER_ASSOCIATIONS.includes(pr.authorAssociation)) return "maintainer";
  if (maintainers.has((pr.author?.login ?? "").toLowerCase())) return "maintainer";
  if (labels.includes(OVERRIDE_LABEL)) return `${OVERRIDE_LABEL} label`;
  if (DECLARED_EXEMPT_TYPE.test(body)) return "declared chore/docs/test";
  if ((pr.additions ?? 0) + (pr.deletions ?? 0) <= TRIVIAL_LINES) return "trivial";
  if (/^\s*revert\b/i.test(pr.title ?? "")) return "revert";
  if (OPT_OUT.test(body)) return "no-issue opt-out";
  return null;
}

const message = (author) =>
  `@${author} This PR doesn't link an issue.

Please edit the description to link the issue this PR addresses with a closing keyword, e.g. \`Closes #123\`, or link it from the **Development** section of the sidebar. Linking gives the PR the issue's priority in our review queue, and closes the issue automatically on merge.

If this change genuinely has no associated issue:

- Check **Refactor / chore**, **Docs**, or **Test / CI** under *Type of change* if that's what it is, or
- Put \`no-issue\` on its own line in the description.

_No action is taken beyond this comment._`;

module.exports = async ({ context, github, core }) => {
  const { owner, repo } = context.repo;
  // Default to a dry run: enforcement is opt-in via the workflow env.
  const enforce = process.env.ENFORCE === "true";
  const limit = Number(process.env.LIMIT || 0) || Infinity;

  try {
    // Load maintainers from the API, not the checked-out tree, so a PR can't
    // self-grant by editing the file (same approach as demo-check.js).
    const maintainers = new Set();
    try {
      const resp = await github.rest.repos.getContent({
        owner,
        repo,
        path: ".github/MAINTAINER",
        ref: "main",
      });
      Buffer.from(resp.data.content, "base64")
        .toString("utf8")
        .split("\n")
        .map((l) => l.replace(/#.*$/, "").trim().toLowerCase())
        .filter(Boolean)
        .forEach((m) => maintainers.add(m));
    } catch (err) {
      core.warning(`Could not load .github/MAINTAINER: ${err.message}`);
    }

    if (enforce) {
      try {
        await github.rest.issues.createLabel({
          owner,
          repo,
          name: LABEL,
          color: "d93f0b",
          description: "PR does not link an issue",
        });
      } catch (err) {
        // 422 = already exists; anything else is unexpected.
        if (err.status !== 422) {
          core.warning(`Could not create label '${LABEL}': ${err.message}`);
        }
      }
    }

    const cutoff = new Date(Date.now() - HOURS_TO_SCAN * MS_PER_HOUR);
    const cutoffString = cutoff.toISOString().replace(/\.\d{3}Z$/, "Z");
    const searchQuery = `repo:${owner}/${repo} is:pr is:open created:>${cutoffString}`;
    console.log(`Scanning PRs: ${searchQuery} (enforce=${enforce})`);

    let cursor = null;
    let hasNextPage = true;
    const allPRs = [];
    while (hasNextPage) {
      const response = await github.graphql(QUERY, { cursor, searchQuery });
      const { remaining, resetAt } = response.rateLimit;
      console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);
      const { nodes, pageInfo } = response.search;
      hasNextPage = pageInfo.hasNextPage;
      cursor = pageInfo.endCursor;
      allPRs.push(...nodes);
    }
    console.log(`Found ${allPRs.length} open PRs from the last ${HOURS_TO_SCAN} hours`);

    const verdicts = [];
    let flagged = 0;

    for (const pr of allPRs) {
      const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
      // Already flagged: the label is the dedupe, so never comment twice.
      if (labels.includes(LABEL)) {
        verdicts.push({ pr: pr.number, verdict: "skip", reason: "already flagged" });
        continue;
      }

      const exempt = exemptReason(pr, maintainers);
      if (exempt) {
        verdicts.push({ pr: pr.number, verdict: "exempt", reason: exempt });
        continue;
      }

      // Authoritative link check: covers closing keywords, cross-repo refs,
      // full issue URLs, and issues linked from the sidebar (which a body
      // regex cannot see and which fires no webhook).
      let linkCount;
      try {
        const resp = await github.graphql(LINK_QUERY, { owner, repo, number: pr.number });
        linkCount = resp.repository.pullRequest.closingIssuesReferences.totalCount;
      } catch (err) {
        // Fail closed: an unverifiable PR is left alone rather than flagged.
        core.warning(`Could not resolve links for #${pr.number}: ${err.message}`);
        verdicts.push({ pr: pr.number, verdict: "skip", reason: "link lookup failed" });
        continue;
      }
      if (linkCount > 0) {
        verdicts.push({ pr: pr.number, verdict: "ok", reason: `${linkCount} linked` });
        continue;
      }

      if (flagged >= limit) {
        verdicts.push({ pr: pr.number, verdict: "deferred", reason: "run limit reached" });
        continue;
      }

      const author = pr.author?.login ?? "contributor";
      verdicts.push({ pr: pr.number, verdict: "FLAG", reason: `@${author}` });
      flagged++;

      if (!enforce) continue;

      // Comment before labeling: if the comment fails the PR stays unlabeled
      // and is retried next run. Labeling first would permanently suppress it.
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pr.number,
        body: message(author),
      });
      await github.rest.issues.addLabels({
        owner,
        repo,
        issue_number: pr.number,
        labels: [LABEL],
      });
    }

    const counts = verdicts.reduce((acc, v) => {
      acc[v.verdict] = (acc[v.verdict] || 0) + 1;
      return acc;
    }, {});
    const summary = Object.entries(counts).map(([k, n]) => `${k}=${n}`).join(" ");
    console.log(`Done (enforce=${enforce}). ${summary}`);

    // The full verdict list, so a dry run can be reviewed before enforcing.
    if (core.summary) {
      core.summary
        .addHeading(`Issue-link check ${enforce ? "(enforcing)" : "(dry run — nothing changed)"}`, 3)
        .addRaw(`\n${summary}\n\n`)
        .addTable([
          [
            { data: "PR", header: true },
            { data: "Verdict", header: true },
            { data: "Reason", header: true },
          ],
          ...verdicts.map((v) => [`#${v.pr}`, v.verdict, v.reason]),
        ]);
      await core.summary.write();
    }
  } catch (error) {
    if (error.status === 429 || error.message?.includes("rate limit")) {
      console.log("Rate limit hit. Exiting gracefully.");
      return;
    }
    throw error;
  }
};

// Exported for the offline unit test.
module.exports.exemptReason = exemptReason;
module.exports.LABEL = LABEL;
