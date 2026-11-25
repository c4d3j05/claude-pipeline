# Design

## Words

- **Window**: the first-parent commits of `origin/<BASE_BRANCH>` that `Tagger._range()` returns today:
  everything after the previous `<TAG_PREFIX><digit>…` tag (at most `WITH_TAG_CAP` = 200 commits), or the
  last `TAG_SCAN_COMMITS` commits when no tag exists. Newest first. Unchanged.
- **Settled status**: a commit status whose context is `explorer/batch-settled` (legacy) or
  `explorer/batch-settled/<lane>` with `<lane>` matching `^[a-z][a-z0-9-]{0,39}$`, and whose description
  matches `^(B-\d+) settled` (`SETTLED_RE`, unchanged). Any other context is ignored.
- **Latest per context**: GitHub returns a commit's statuses newest first; for each context only the first
  one in the list counts (today's rule, now applied per context). A `failure` that is the latest of its
  context on a commit hides an older `success` of the same context on that commit.
- **Batch release**: a merged release PR whose body has a line `Batch: B-<n>` and whose merge commit is in
  the window.

## Reading

`_statuses(sha)` keeps calling `gh api repos/{owner}/{repo}/commits/<sha>/statuses?per_page=100` once per
commit. Only the first 100 statuses of a commit are read; a commit with more than 100 statuses may hide an
older context, which is accepted (explorer posts a handful per commit).

New pure function:

```python
def settled_on(statuses: list[dict]) -> dict[str, str]:
    """{batch id: lane} for every context whose latest status on this commit is a success.
    lane is "" for the legacy context."""
```

Example: statuses (newest first)
`[{"context": "explorer/batch-settled/billing", "state": "success", "description": "B-4 settled; check passed: …"},
  {"context": "explorer/batch-settled/portal", "state": "failure", "description": "B-6 settled; check failed: 1 script"},
  {"context": "explorer/batch-settled", "state": "success", "description": "B-1 settled: 3 findings"},
  {"context": "ci/test", "state": "success", "description": "ok"}]`
→ `{"B-4": "billing", "B-1": ""}`.

`settled_batches(statuses)` stays, with its current signature and behaviour (legacy context only), because
`tests/test_tagging.py::test_only_the_latest_status_of_the_context_counts` uses it.

New method on `Tagger`:

```python
def _release_prs(self) -> tuple[list[dict] | None, str]:
    """Merged release PRs, or None and why."""
```

It runs, through `self._gh` (so the tests' fake runner sees it):
`gh pr list --label <cfg.release_pr_label> --state merged --json number,headRefName,mergeCommit,body --limit 100`.
A non-zero exit or unparsable JSON returns `(None, "could not list the release PRs: <stderr, 300 chars>")`,
which `plan()` returns as its error (alerted once and retried like any other error). Only the newest 100
merged release PRs are seen; older ones are outside any realistic window.

From each PR: `release` = `headRefName` `release/r-6` → `R-6` (upper-cased text after `release/`);
`merge` = `mergeCommit["oid"]` (full SHA; skip the PR when missing); `batch` = first match of
`Batch:\s*(B-\d+)` in `body` (skip the PR when there is none: releases opened before this change, and
releases that came from a DM and have no batch, never block). A PR whose `merge` is not in the window is
skipped (merged before the previous tag, or not on the first-parent history).

## The rule

Let the window be `c[0]` (newest) … `c[k]` (oldest), and `S[i] = settled_on(statuses of c[i])`.

- A batch release with batch `b` merged at `c[m]` is **covered at** `c[i]` (`i <= m`, that is `c[i]` is
  the merge or newer) when `b` is a key of `S[j]` for some `j` with `i <= j <= m`.
- `c[i]` is a **candidate** when `S[i]` is not empty (explorer checked this very commit).
- The commit to tag is the newest candidate `c[i]` at which every batch release with `m >= i` (merged at
  `c[i]` or earlier in the window) is covered. Candidates are tried newest first; the first that passes wins.
- No candidate passes → nothing to tag.

Consequences, stated so nobody has to derive them:

- With no merged batch release in the window the rule is today's rule: the newest commit with a success.
- A success for `B-4` posted on a commit *before* `R-4`'s merge does not cover `R-4`.
- A later `failure` on a *different* commit does not undo an earlier success between the merge and the
  candidate.
- The lane is never compared; coverage is by batch id only. The lane is shown.
- A direct merge into `main` (a DM ticket, a hotfix) is not a batch release and never blocks.

`Plan.batches` = every batch id found in `S[j]` for `j >= i` (the candidate and older), sorted by number,
as today. `Plan.lanes` = `{batch: lane}` for those ids; when the same batch appears with a lane and without,
the non-empty lane wins.

## Shapes

```python
@dataclass
class Plan:
    name: str
    sha: str
    batches: list[str]
    previous: str
    notes: str
    commits: list[str] = field(default_factory=list)
    lanes: dict[str, str] = field(default_factory=dict)      # new; batch id -> lane, "" = legacy

@dataclass
class Outcome:
    plan: Plan | None
    tagged: bool = False
    url: str = ""
    error: str = ""
    blockers: list[str] = field(default_factory=list)        # new; one line per blocking release
```

`label(batch, lanes)` → `"B-4 (billing)"` when `lanes.get(batch)` is non-empty, else `"B-4"`. It is used in
three places, joined with `", "`:

- notes header (`render_notes`): `B-4 (billing), B-5 (accounts-contacts) settled on UAT at \`<sha9>\` (explorer \`explorer/batch-settled\`: every finding resolved or dismissed, release check passed).`
  With legacy statuses only this must still start `B-1, B-2 settled on UAT at \`bbbbbbbbb\`` (an existing test asserts that prefix).
- annotated tag message (`_create`): `B-4 (billing), B-5 (accounts-contacts) settled on UAT (explorer/batch-settled)`
- Slack (`_publish`): `:label: Tagged v2026.11.24 on <sha9>: B-4 (billing), B-5 (accounts-contacts) settled · <url>`

Pending plan (`_set_pending` / `_pending`, ledger meta key `tag_pending`): the JSON gains `"lanes"`.
`_pending` reads it with `d.get("lanes") or {}` so a plan stored before this change still loads.

## Blockers

When a candidate fails the rule, one line is recorded for each uncovered batch release, built from the
**newest** candidate only (older candidates' reasons are not listed):

`blocked by R-6 (B-6, PR #650, merged 1a2b3c4d5): no successful explorer/batch-settled status for B-6 since its merge`

- `plan()` returns the lines through a new third element: `plan() -> tuple[Plan | None, str, list[str]]`.
  They are returned whether or not an older candidate ends up tagged.
- `run_pass()` copies them to `Outcome.blockers`, and logs each at info (`log.info("release tag: %s", line)`).
  It never posts them to Slack and never alerts for them: a blocked tag is a normal waiting state.
- `describe()` prints them: when `plan is None` and there are blockers, the output is
  `Nothing to tag: every commit with a settled batch also contains a release that is not settled yet.`
  followed by the blocker lines, one per line; when a plan exists, the lines are appended after the notes
  under a line `Not included (newer commits):`. `claude-pipeline tag` keeps exit code 0 unless
  `Outcome.error` is set (unchanged in `cmd_tag`, `dispatcher/__main__.py:227-240`).

## The release PR body

`_release_body(rel, tracker, subs, link)` (`dispatcher/daemon.py:1223`) gains a keyword `batch: str = ""`.
When non-empty it inserts the line `Batch: B-<n>` directly after the `Tracker: #…` line. Its one caller is
in `release_pass` (the `prmod.create_release_pr(...)` call); there the tracker's description is read with
`self.vk.task(t["id"])` (the call that already fetches the subtasks; reuse its result rather than calling
twice) and passed through a new helper in `dispatcher/releases.py`:

```python
BATCH_LINE = re.compile(r"Batch:\s*(B-\d+)\b")

def batch_of(description: str | None) -> str:
    """The batch id on a release tracker's `Batch: B-n` line, or ""."""
```

Example body after the change:

```
Release R-6: every ticket below is merged into `release/r-6` and the head passed the test gate.

**Merge with a merge commit, not a squash**: explorer checks each ticket's merge commit is on UAT, and a squash hides them.

Tracker: #861 (https://vikunja.example/tasks/861)
Batch: B-6

Tickets:
- #862 Fix the invoice total
```
