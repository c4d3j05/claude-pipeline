# Design

## Signal

explorer → pipeline goes through a GitHub commit status, not Slack (pipeline does not read Slack)
or Vikunja (explorer only reads it). explorer already posts `explorer/release-verdict` with a
statuses-only token; `explorer/batch-settled` uses the same token:
- context `explorer/batch-settled`, state `success`
- description `B-n settled: …` (the batch id is parsed from `^(B-\d+) settled`)
- on the UAT commit explorer verified when the last finding settled

Only the latest status of that context on a commit counts (GitHub lists statuses newest first).

## Which commit

At most every `TAG_POLL_MIN`, in the worker pool (never two passes at once):
1. `git fetch --tags origin +refs/heads/<base>:refs/remotes/origin/<base>`
2. previous = `git describe --tags --abbrev=0 --match '<prefix>[0-9]*' origin/<base>`
3. range = `git rev-list --first-parent origin/<base> ^previous` (at most 200), or the last
   `TAG_SCAN_COMMITS` first-parent commits when there is no tag yet
4. `gh api repos/{owner}/{repo}/commits/<sha>/statuses` for each, newest first

The newest settled commit is tagged; every settled batch in the range goes in the notes. Only
first-parent commits of origin/<base> are ever candidates.

## Name, tag and release

- name: `<prefix>YYYY.MM.DD` in the host's local date, then `.2`, `.3`… while that tag exists on
  another commit (a tag of ours already on the chosen commit is reused)
- `git tag -a <name> <sha> -m "<batches> settled on UAT (explorer/batch-settled)"` with the
  pipeline's identity; `git push origin refs/tags/<name>`
- `gh release create <name> --verify-tag --title <name> --notes <notes>` (skipped when
  `gh release view` finds one)
- notes: a header with the batches and the short SHA, then `## Merged`: one line per first-parent
  commit from the tagged one back to the previous tag, `#N <PR title>` for a PR merge
  (`Merge pull request #N from …`, title from the message body), else the subject
- Slack: `:label: Tagged <name> on <sha9>: <batches> settled · <release url>`

## Failure

Every git and gh call is read by its returncode; nothing raises into the daemon. A failure is
logged and alerted once per commit (`meta` key `tag_alert:<sha>`), then retried on the next poll.
Once the tag exists locally it is the newest tag, so the scan alone would skip its commit: the plan
is kept in `meta` (`tag_pending`) and the next pass finishes it (push, release, Slack) before
scanning. A tag is never moved or deleted.

## Dry run

`claude-pipeline tag --dry-run` fetches (read-only towards GitHub) and prints the name, commit,
batches and notes; it creates no tag, release, Slack post or ledger row. A real `claude-pipeline tag`
refuses while `RELEASE_TAGS` is off.
