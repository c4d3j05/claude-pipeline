# Claude Worker Pipeline — Spec

_Markdown transcription of `claude_worker_pipeline.pdf` (Sep 25, 2026)._

## Goal

A Vikunja task marked ready becomes a reviewed pull request without anyone at a
terminal. One Claude Code worker per task, N tasks in parallel on one Mac, each in its
own git worktree with its own ports and database namespace.

**What it does:**
- Picks up tasks from Vikunja, runs a Claude Code session per task, and writes status back.
- Refuses to open a PR until the branch's tests pass and a second review pass has run.
- Notifies on "needs input" and "done"; caps spend per task and per day.

**What it deliberately does not do:**
- Merge. A human merges every PR.
- Coordinate workers. Tasks are assumed independent; conflicts surface at merge.
- Run in the cloud. Everything executes on the developer's Mac.

## Architecture

Six components, all local. The dispatcher is the only new code; the rest is Claude
Code, ntree, gh and hooks wired together.

```
Vikunja task (status=ready) --> Dispatcher (Python daemon)
Dispatcher --> ntree new slug (worktree + ports + deps)
Worker --> claude -p (unattended session)
Worker --> Verification gate (tests + review)
gate --green--> gh pr create --> Human merge --> ntree rm (cleanup)
gate --red--> back to worker
```

### Task flow
1. Dispatcher sees a task in the `ready` bucket and claims it (moves to `in-progress`, records PID).
2. `ntree new <slug> --from main` creates the worktree, reserves ports, runs `NTREE_SETUP_CMD`.
3. Dispatcher launches `claude -p` inside the worktree with the task prompt, a turn cap, and `--output-format stream-json`.
4. A Stop hook runs the test command via `ntree run <slug> -- <tests>`; failure returns the session to work.
5. On green, a second `claude -p` reviews the diff read-only and posts findings; blocking findings send it back once.
6. `gh pr create` from the worktree, body linking the Vikunja task and session ID. Task moves to `review`.
7. On PR merge or close, dispatcher runs `ntree rm <slug>` and moves the task to `done` or `blocked`.

### Components
| Component | What it is | Owns |
|---|---|---|
| Dispatcher | Python daemon, ~200 lines, systemd/launchd | Vikunja polling, prompt build, session launch, budget, notifications |
| Worker | `claude -p` with settings.json + hooks | The change, commits, PR body draft |
| Workspace | ntree worktree | Files, ports, `.venv`, DB namespace |
| Gate | Stop hook + review session | Test result, review verdict |
| PR step | `gh` CLI | Branch push, PR creation |
| Cleanup | Dispatcher on merge + nightly `ntree doctor` | Worktrees, ports, orphan DBs |

## Dispatcher

A single Python process that turns Vikunja tasks into Claude Code sessions and keeps
Vikunja truthful. Poll first; add a webhook later if latency matters.

- **Intake.** Poll the Vikunja API every 60s for tasks with label `claude` and bucket
  `ready`. Claim by moving to `in-progress` and writing a comment
  `dispatched: session=<id> pid=<pid> workspace=<slug>`. A task with an existing
  `dispatched:` comment is never re-claimed.
- **Prompt construction.** Title + description + checklist items, plus a fixed preamble:
  branch/slug from task id (`task-123-short-title`); "Commit as you go. Do not push. Do
  not open a PR"; "If blocked, write BLOCKED: <reason> and stop"; acceptance criteria verbatim.
- **Launch.** From inside the worktree:
  ```
  claude -p "$PROMPT" --output-format stream-json --max-turns 60 \
    --permission-mode auto --settings .claude/worker-settings.json
  ```
- **Concurrency.** `MAX_WORKERS=4` by default; a fifth ready task waits. Each worker is a
  subprocess the dispatcher owns, so a restart reaps orphans by PID.

### Status write-back
| Event | Bucket | Comment |
|---|---|---|
| Claimed | in-progress | `dispatched: …` |
| Tests failed (retry n) | in-progress | `gate: tests red, retry n/2` |
| Review blocked | in-progress | `gate: review blocking, retry` |
| PR opened | review | `pr: <url>` |
| Worker wrote BLOCKED | blocked | `blocked: <reason>` |
| Turn/budget cap hit | blocked | `cap: turns=60 or cost=$X` |
| PR merged | done | `merged: <sha>` |
| PR closed unmerged | blocked | `closed without merge` |

### Budget caps (enforced by the dispatcher, not the model)
- Per task: `--max-turns 60` and a wall-clock limit of 45 min. Either hit → `blocked`.
- Per task retries: at most 2 test-red retries and 1 review retry, then `blocked`.
- Per day: sum the `cost_usd` from each session's final result event; stop dispatching at
  `DAILY_CAP_USD` and post once to Slack.

**State.** One SQLite file: task id, session id, workspace, PID, started_at, cost, state.
Vikunja is the source of truth for the queue; SQLite is only for restart recovery and the cost ledger.

## Worker isolation

Each task gets a real checkout, its own ports, and its own database name.

- **Worktree.** `ntree new <slug> --from main` under `.ntree/<slug>/`. Shared `.git`.
  The dispatcher creates and removes worktrees; Claude is launched with the worktree as
  cwd and never told about `.ntree/`.
- **Ports.** `.ntreerc` per repo (`NTREE_SETUP_CMD`, `NTREE_PORT_RANGE_*`, `NTREE_PORTS`).
  ntree writes `.env.workspace` with `PORT`, `NTREE_WORKSPACE`, etc.
- **Database.** Settings read `NTREE_WORKSPACE` and derive a DB name (Django:
  `NAME = f"app_{os.environ.get('NTREE_WORKSPACE','dev')}"`). Setup command creates it.
- **Dependencies.** `uv sync` (or `npm ci`) per workspace via `NTREE_SETUP_CMD`.
- **Shared:** git objects, the Postgres server, the Anthropic auth token, `~/.claude/`.
  Nothing else. Secrets come from `.env.workspace` or the repo, never another workspace.
- **Portability.** macOS only for phase 1; a 50-line shell equivalent honours the same
  contract on Linux later. Keep the dispatcher talking to the contract, not ntree's CLI.

## Unattended permissions

Auto mode inside the sandbox with a short deny list. Every prompt path must be closed
before the first real run (a permission prompt with nobody there is a stalled task).

- **Mode.** `--permission-mode auto`; server-side classifier approves ordinary edits and
  blocks destructive ones. Fallback to an allow list in `-p` mode if auto is unavailable.
- **Sandbox.** Filesystem writes limited to the worktree; network limited to the package
  registries, the Anthropic API, and the local Postgres port. See
  `.claude/worker-settings.json`.
- **Deny list is the contract:** the worker cannot push, cannot open PRs, cannot touch the
  pipeline's config, cannot run ntree. Those belong to the dispatcher and the gate.
- **Protected paths.** `.env*`, `secrets/`, anything under `infra/` are read- and write-deny.
- **Failure behaviour.** A blocked action is a permission denial in stream-json. Three in
  one session → `blocked` with the denied command in the comment.

## Verification gate

Two checks between "the worker says it is done" and a PR: the branch's own tests, then a
review by a session that did not write the code. Both mechanical.

- **Tests as a Stop hook** in `.claude/worker-settings.json`:
  ```
  ntree run $NTREE_WORKSPACE -- uv run pytest -x -q 2>&1 | tail -40; exit ${PIPESTATUS[0]}
  ```
  A non-zero exit blocks the stop and feeds the tail back as the next turn. The Stop hook's
  block cap is higher than the dispatcher's retry limit (2), so the dispatcher decides.
- **Working-tree check.** Before review, `git status --porcelain`; uncommitted changes → one
  more turn with "commit or discard".
- **Adversarial review.** A second `claude -p`, read-only (`--permission-mode plan`), given
  the task description + acceptance criteria and `git diff main...HEAD`, instructed: "Find
  reasons this should not merge. Output JSON {verdict: pass|block, findings:[…]}. Block only
  on correctness, security, or a missed acceptance criterion; style is a note." `--json-schema`
  enforces the shape. A `block` sends findings back for one retry; a second `block` → `blocked`.
  `pass` findings go into the PR body under "Review notes". Review runs a cheaper model.
- **What the gate does not do.** It does not run the app or click through it. If a task needs
  manual QA, the task says so and the PR gets a `needs-qa` label.

## PR creation and merge policy

The dispatcher pushes and opens the PR; the worker never does. A human merges.

```
git push -u origin task-123-short-title
gh pr create --base main --title "<task title>" --body-file pr-body.md --label claude-worker
```

`pr-body.md` (assembled by the dispatcher): Vikunja task link + id; acceptance criteria as
a checklist ticked by the review verdict; worker's own summary (final message, ≤300 words);
review notes; session id and cost.

- **Merge policy.** Human merges, always. CI is the third check. Squash-merge.
- **Follow-ups.** PR review comments are not handled in phase 1 (phase 2/3).

## Cleanup

- **On PR merge or close.** Dispatcher polls open `claude-worker` PRs every 5 min. On close:
  1. `ntree rm <slug> --force` (stops process, frees ports, removes worktree).
  2. `dropdb app_<slug>` and flush the matching Redis prefix.
  3. `git branch -D <branch>` locally; remote branch deleted by GitHub auto-delete.
  4. Vikunja task → `done` (merged) or `blocked` (closed unmerged), with the outcome as a comment.
- **Nightly** (launchd at 03:00): `ntree doctor`; list workspaces older than 7 days with no
  open PR and post to Slack; drop `app_task-*` DBs with no live workspace; rotate logs.
- **Disk budget.** Alert when `.ntree/` passes 10 GB.

## Project context (CLAUDE.md)

A repo is not eligible until its `CLAUDE.md` answers, in under 150 lines:

| Section | Must say |
|---|---|
| Layout | Where apps/services/models/tests/migrations live; what is generated and must not be edited |
| Run tests | The exact command the Stop hook uses, and how to run one file |
| Conventions | Formatter, linter, type-checker, commit message shape, branch naming |
| Isolation | That `NTREE_WORKSPACE` and `$PORT` exist and must be used for binds/names |
| Do not touch | Existing migrations, `infra/`, `.env*`, vendored code, the pipeline's `.claude/` files |
| Definition of done | Tests pass, no new lint errors, acceptance met, changes committed, nothing pushed |
| Blocked protocol | Write `BLOCKED: <reason>` and stop; never guess a missing requirement |

## Observability and notifications

- **Stream.** `--output-format stream-json`, raw stream kept at `.ntree/<slug>/session.jsonl`.
  Extract: rolling last assistant message; tool-use count; permission denials; final result
  (`cost_usd`, `duration_ms`, `num_turns`) and the summary for the PR body.
- **Vikunja.** One comment per state change. No per-turn comments.
- **Slack.** Single channel `#claude-workers`: task blocked, PR opened, daily cap, nightly summary.
- **Dashboard.** Deferred; `sqlite3 pipeline.db` and `ntree list` cover phase 1.
- **Transcripts.** Full session transcripts stay in `~/.claude/projects/`.

## Rollout

- **Phase 1 — one repo, on the laptop.** Target: finops (Python, FastAPI, small test suite).
  Order: `.ntreerc`/`NTREE_WORKSPACE`-aware DB naming + `CLAUDE.md`; `worker-settings.json`;
  Stop hook with tests; dispatcher (poll/claim/launch/write-back/caps + SQLite ledger); review
  session + PR creation; cleanup + nightly + Slack. Exit: 10 tasks dispatched, ≥7 merged with no
  manual fix-up, no task stalled on a prompt, daily cost under the cap.
- **Phase 2 — second repo, harder isolation.** Kippr's Django backend. Per-workspace DB
  creation in the setup command, `needs-qa` labelling, PR-comment follow-up as new tasks.
- **Phase 3 — every personal repo, plus PR follow-ups.** A review comment on a `claude-worker`
  PR becomes a new Vikunja task pointing at the branch (resume the same worktree).

### Decided
- Personal projects only; no gateway, auto mode is available.
- Vikunja bucket moves are the state signal; labels only mark eligibility (`claude`).
- Postgres per workspace via `createdb` in the setup command.
- Review session runs a cheaper model than the worker, with the stricter JSON-schema prompt.
- No ntree fork; Linux portability not needed while everything runs on the Mac.

### Out of scope (all phases)
- Agent teams and cross-session messaging (tasks are independent by design).
- Cloud sessions and Claude Projects.
- Auto-merge (until phase 3, and then only for finops).
