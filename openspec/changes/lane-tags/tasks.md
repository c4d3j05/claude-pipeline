Do the tasks in order. Every test named in a task must pass at the end of that task, together with every
test from the tasks before it: run `uv run pytest tests/test_tagging.py tests/test_releases.py` and
`uv run ruff check dispatcher tests` after each task. The full suite has a known intermittent `git init` crash on macOS
in unrelated tests (`tests/test_gaps.py`); rerun once before treating a failure there as yours.

## 1. The release PR names its batch
- [ ] 1.1 `dispatcher/releases.py`: add `BATCH_LINE` and `batch_of(description)` exactly as in design.md
      ("The release PR body"). Test `tests/test_releases.py::test_batch_of_reads_the_tracker_line`:
      `batch_of("Batch: B-6\n\nRelease R-6: …") == "B-6"`, `batch_of("<p>Batch: B-12</p>") == "B-12"`,
      `batch_of("no line") == ""`, `batch_of(None) == ""`.
- [ ] 1.2 `dispatcher/daemon.py`: `_release_body(rel, tracker, subs, link, batch="")` adds `Batch: <batch>`
      right after the `Tracker:` line when `batch` is non-empty. In `release_pass`, keep the result of
      `self.vk.task(t["id"])` in a variable, read `subs` from it as today, and pass
      `batch=releases.batch_of(task.get("description"))` to `_release_body`.
      Test `tests/test_releases.py::test_the_release_pr_body_names_the_batch`: with `batch="B-6"` the body
      contains the line `Batch: B-6` immediately after the line starting `Tracker: #`; with `batch=""` the
      body has no `Batch:` line and equals today's output.
      Test `tests/test_releases.py::test_release_pass_puts_the_trackers_batch_in_the_pr_body`: reuse the
      fixture of the existing release-pass test at `tests/test_releases.py:398` (its tracker description
      already has `Batch: B-1`); after `release_pass()` the body passed to `prmod.create_release_pr`
      contains the line `Batch: B-1`. This is the only test of the wiring, so do not skip it.

## 2. Tagging
- [ ] 2.1 `tests/test_tagging.py`: extend `FakeRepo` so the existing tests keep passing once `plan()` lists
      release PRs: a constructor argument `prs=()` (list of dicts with `number`, `headRefName`,
      `mergeCommit: {"oid": sha}`, `body`) and, in `__call__`, for `tool == "gh"` and `rest[:2] == ["pr", "list"]`
      return `ok(json.dumps(list(self.prs)))`. `"pr list"` in `fail` must make it fail like other calls.
      Also let `_status(...)` take `lane=""` and build the context `tg.CONTEXT + "/" + lane` when set.
- [ ] 2.2 `dispatcher/tagging.py`: add `LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")` and
      `settled_on(statuses)` as in design.md ("Reading"). Leave `settled_batches` untouched.
      Test `test_settled_on_reads_every_lane_context_latest_first`: the example in design.md gives
      `{"B-4": "billing", "B-1": ""}`; a context `explorer/batch-settled/Bad_Lane` is ignored; two statuses of
      the same lane context with the newer one `failure` give nothing for that context.
      Test `test_one_batch_under_several_contexts_failure_wins`: the four rows of the table in design.md
      ("One batch under several contexts"), asserting each result exactly.
- [ ] 2.3 `Tagger._release_prs()` as in design.md (`plan()` does not call it yet; that is 2.5).
      Test `test_release_prs_are_read_through_gh`: with `FakeRepo(prs=[R6])`, `Tagger._release_prs()` returns
      `([R6], "")` and `repo.calls` contains a `gh pr list` call with `--state merged`; with
      `FakeRepo(fail=("pr list",))` it returns `(None, <text starting "could not list the release PRs">)`.
- [ ] 2.4 `Plan.lanes`, `Outcome.blockers`, `label()`, and the three message formats in design.md ("Shapes").
      `_set_pending` stores `lanes`; `_pending` reads `d.get("lanes") or {}`.
      Test `test_an_old_pending_plan_without_lanes_still_loads`: put
      `{"name": "v2026.11.24", "sha": A, "batches": ["B-1"], "previous": "", "notes": "n"}` in the ledger
      under `tag_pending` with the tag present locally on `A`; `run_pass()` publishes it (`tagged`), and
      `plan.lanes == {}`.
- [ ] 2.5 `Tagger.plan()` implements "The rule" in design.md and returns `(plan, error, blockers)`; update
      its one caller `run_pass()`. Read statuses for the whole window first (as today), then the release PRs
      (only when at least one candidate exists, so a window with no success makes no `gh pr list` call).
      Fixture PR numbers, used wherever these releases appear: R-1 #641, R-2 #642, R-4 #644, R-5 #645,
      R-6 #650; each head is `release/r-<n>`.
      Tests (commits newest first are `A, B, C` as in the file):
      - `test_an_unreadable_pr_list_is_an_error`: `FakeRepo(fail=("pr list",), statuses={A: [_status()]})`
        → `run_pass()` returns `error` starting `could not list the release PRs`, `blockers == []`, writes nothing.
      - `test_one_lane_settled_while_another_release_is_merged_blocks_the_tag`: prs = R-5 (`Batch: B-5`,
        merged at `C`) and R-6 (`Batch: B-6`, merged at `B`); statuses `{A: [_status(desc="B-5 settled", lane="accounts-contacts")]}`.
        In this task assert on `tagger.plan()` directly: it returns `(None, "", blockers)` with `blockers ==
        ["blocked by R-6 (B-6, PR #650, merged bbbbbbbbb): no successful explorer/batch-settled status for B-6 since its merge"]`
        (PR number 650 in the fixture); and `run_pass()` tags nothing, returns no error, writes nothing and
        posts nothing to Slack. `Outcome.blockers` is not asserted here: task 2.6 adds that field and extends
        this test with `out.blockers == blockers`.
      - `test_both_lanes_settled_tags_and_names_both`: same prs; `A` carries success for B-5
        (accounts-contacts) and B-6 (billing). Tagged on `A`; `plan.batches == ["B-5", "B-6"]`;
        `plan.lanes == {"B-5": "accounts-contacts", "B-6": "billing"}`; notes start
        `B-5 (accounts-contacts), B-6 (billing) settled on UAT at \`aaaaaaaaa\``; the Slack post contains
        `B-5 (accounts-contacts), B-6 (billing) settled`.
      - `test_an_older_covered_commit_is_tagged_when_the_newest_is_blocked`: prs = R-5 merged at `C`, R-6
        merged at `A`; `B` has success B-5, `A` has success B-5 only. Tagged on `B` (R-6 is merged after it);
        the third element returned by `tagger.plan()` names R-6, and `Outcome.blockers == []` (see 2.6).
      - `test_a_legacy_status_covers_its_own_batch`: prs = R-1 (`Batch: B-1`) merged at `B`; `A` has the
        legacy `_status(desc="B-1 settled")`. Tagged on `A`; `plan.lanes == {"B-1": ""}`; notes start `B-1 settled`.
      - `test_a_status_before_the_merge_does_not_cover`: R-4 (`Batch: B-4`) merged at `A`; `B` has success
        B-4 (billing) and `A` has success B-9 (portal). `A` is not tagged (B-4's success is older than R-4's
        merge) and the third element returned by `tagger.plan()` names R-4; `B` is tagged instead, because R-4 is not merged at `B` yet.
      - `test_a_release_pr_without_a_batch_line_never_blocks`: prs = R-2 with body `Tracker: #841` merged at
        `B`; `A` has success B-9 (billing). Tagged on `A`.
      - `test_a_failure_status_alone_tags_nothing`: `A` has `_status(state="failure", desc="B-4 settled; check failed", lane="billing")`.
        `plan is None`, no blockers, and no `gh pr list` call in `repo.calls`.
      - `test_two_releases_of_one_batch_each_need_coverage`: prs = R-5 (`Batch: B-5`, merged at `C`) and a
        second PR #651 head `release/r-7` (`Batch: B-5`, merged at `A`); `B` has success B-5. `B` is tagged
        (the second release is merged after it); with the success moved to `A` instead, `A` is tagged.
      - `test_a_release_pr_with_another_head_still_counts`: prs = PR #652 head `hotfix-totals`
        (`Batch: B-8`, merged at `B`); `A` has success B-9 (billing). `plan is None` and the blocker line starts
        `blocked by hotfix-totals (B-8, PR #652, merged bbbbbbbbb)`.
      - Every test that existed before this change still passes without edits to its assertions.
- [ ] 2.6 `run_pass()` logs blockers at info and sets `Outcome.blockers` only when nothing is tagged, per
      the table in design.md ("Blockers"); `describe()` prints exactly the outputs listed there.
      Extend `test_one_lane_settled_while_another_release_is_merged_blocks_the_tag` (from 2.5) with
      `run_pass().blockers ==` the same one-line list.
      Test `test_dry_run_prints_the_blockers`: the fixture of
      `test_one_lane_settled_while_another_release_is_merged_blocks_the_tag` with `run_pass(dry_run=True)`;
      `tg.describe(out)` equals the two-line "Every candidate blocked" output in design.md; nothing is written.
      Test `test_blockers_are_not_reported_when_an_older_commit_is_tagged`: the fixture of
      `test_an_older_covered_commit_is_tagged_when_the_newest_is_blocked`; `out.tagged`, `out.blockers == []`,
      and `tg.describe(out)` has no line starting `blocked by`.
- [ ] 2.7 Update the module docstring of `dispatcher/tagging.py` and the "Release tags" section of
      `README.md` to describe the per-lane contexts, the rule, and the blocker lines.

## 3. Deploy (not code; the owner does these)
- [ ] 3.1 Pull on the Debian host and restart `claude-pipeline-dispatcher` (it drains running tickets first).
- [ ] 3.2 `uv run claude-pipeline tag --dry-run` shows the expected plan or blockers.
- [ ] 3.3 Once explorer's `lane-tags` change is live, set `RELEASE_TAGS=true`.
- [ ] 3.4 Before archiving: report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`.
