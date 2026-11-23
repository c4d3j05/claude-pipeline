# Design

## One gate

`auto_merge_allowed(*, tickets, to_main, head, base, base_branch)` replaces
`auto_merge_allowed(enabled, base)`.

| Case | Rule |
|---|---|
| `base` is a release branch | allowed only when `tickets` is on, and `head` is not itself a release branch |
| otherwise | allowed only when `to_main` is on, `base == base_branch` (non-empty) and `head` is a release branch |

A ticket PR into main therefore never passes: its head is `task-<id>-…`, and callers merging ticket
PRs pass `head=None`. A test covers every combination.

Both merge helpers call the gate first and make no gh call when it says no:
- `pr.merge_into_release` for ticket PRs
- `pr.merge_release_into_main` for the release PR

## Merging the release PR

In `release_pass`, right after `create_release_pr`, when `RELEASE_MERGE_TO_MAIN` is on:
1. `gh pr merge <url> --merge --match-head-commit <origin/release/r-n sha>`. `--match-head-commit`
   merges exactly the head the release gate tested.
2. If that fails, `gh pr merge <url> --merge --auto --match-head-commit <sha>`: GitHub merges it once
   main's required checks pass. This avoids parsing gh's error text to tell "checks pending" from
   other refusals.
3. If both fail, the outcome is `refused`.

Never `--squash` or `--rebase`: explorer traces each ticket's merge commit on UAT through the release
merge.

**No retries.** The next pass sees an open release PR and skips that release, as it already did. A
refused release waits for the owner, who is told once.

**Messages.** `releases.merge_messages(rel, url, base, outcome, why)` returns the tracker comment and
the Slack alert for `merged`, `queued`, `refused` and `off`; `off` is today's text.

**Closing.** Once GitHub reports the PR merged, the existing close path runs on the next pass
(within 5 minutes): tracker → Done, `merged: <sha9> into main (<url>)`, branch deleted.

## Not changed

- A release PR opened before the setting was turned on stays for a human: the pass skips open
  release PRs.
- `spec.md` is not edited ("Do not touch"). RELEASES.md and README.md record the owner's decision.
