# Authoring tasks for the Claude Worker Pipeline

How to write Vikunja tasks so a batch runs **in parallel safely** and each task produces a
clean, reviewable PR. These rules are repo-agnostic; target repos add their own specifics
(e.g. kauby: `.ai/05-workflows/claude-pipeline-tasks.md`).

## How a task is picked up
The dispatcher polls **one** Vikunja project's Kanban board. A task is eligible when **all**
of these hold:
- it carries the **`claude`** label,
- it is in the **`Ready`** bucket,
- it has no `dispatched:` comment yet.

It is then claimed (→ *In Progress*), a worker runs in a fresh worktree, the **test gate**
and the **review** must pass, and a PR opens (→ *Review*). A human merges; cleanup then moves
it to *Done*. Labels only mark eligibility — **bucket moves are the state signal.**

## Parallelism: what's automatic, what's on you
**Automatic (infra):** every task gets its own git worktree, its own database namespace, and
its own ports. Parallel workers never collide **at runtime**.

**Your job:** parallel branches collide at **merge**, not during work. Scope a batch so it is
merge-safe:

1. **Disjoint blast radius.** Tasks running together should touch *different* files/modules.
   Two tasks editing the same file will merge-conflict.
2. **One migration-producing task per data area in flight.** Parallel tasks that each generate
   a schema migration collide (duplicate migration numbers / broken graph). Serialize them, or
   keep migrations out of parallel batches.
3. **Independently mergeable + testable.** Each task's Definition of Done must be verifiable on
   its own branch with its own tests. No task may depend on another task's *unmerged* branch.
4. **Coordination → one task.** If two units of work must share a change or land together, they
   are a single task, not two. The pipeline does not coordinate workers.

Rule of thumb: *if you can't merge the two PRs in either order without a conflict, they
shouldn't run in parallel.*

## Ticket structure
**Title** — short, imperative; it becomes the branch `task-<id>-<slug>`, so keep it concise.

**Description** — these sections (the worker reads all of them; the review checks the last):
- **Purpose** — one paragraph: what and why.
- **Contract** — the exact interface/behavior: endpoints, function signatures, payloads, states.
- **Business rules & edge cases** — bullets.
- **Dependencies** — what it relies on, or "none".
- **Definition of Done** — a checklist of **checkable** acceptance criteria. The worker copies
  these verbatim, the review verifies them, and the PR body ticks them. Always include a tests item.
- **Reference** (optional) — relevant files/paths, to save the worker discovery time.

## Scope & quality
- **Small and single-purpose.** One concern per task.
- **Test-backed.** The gate runs the repo's test command; a task with no way to prove it works
  will struggle to pass.
- **Checkable criteria** (status codes, exact outputs, specific behavior) — not "make it nice".
- **Flag manual QA** explicitly (write "manual QA" in the description) → the PR gets a `needs-qa`
  label. The gate does not run or click through the app.

## What the worker cannot do — don't write tasks that need these
- Push, open PRs, run `ntree`/`rm -rf`, or edit `.claude/`, `.github/`, `.env*`, `secrets/`,
  `infra/` (deny list). Config/infra changes are not pipeline tasks.
- Reach arbitrary network hosts — only the configured package registries + the Anthropic API
  (sandbox). A task needing a brand-new external dependency won't work as-is.
- Modify existing migrations — new migrations only.

## Blocked protocol
If a requirement is missing or ambiguous, the worker writes `BLOCKED: <reason>` and stops — it
never guesses. A vague ticket wastes a run. **Over-specify rather than under-specify.**

## Budget & caps (so you know the limits)
Per task: a turn cap and a wall-clock limit; either → *Blocked*. Retries: ≤2 test-red, ≤1 review.
Per day: a spend cap that pauses dispatching. Keep tasks small to stay well inside these.

---

## Task template

Copy this into a new task's description and fill it in (drop sections that don't apply):

```markdown
### Purpose
<one paragraph: what this delivers and why>

### Contract
<exact interface/behavior — endpoints, signatures, payloads, states>

### Business rules & edge cases
- <rule>
- <edge case>

### Dependencies
<what it relies on, or "none">

### Definition of Done
- [ ] <checkable acceptance criterion>
- [ ] <checkable acceptance criterion>
- [ ] A test asserts the behavior and passes under the repo's test command.
- [ ] No new lint errors; no changes to existing migrations; nothing pushed.

### Reference
<relevant files/paths, optional>
```

Then: add the **`claude`** label and move the task to **`Ready`**.
