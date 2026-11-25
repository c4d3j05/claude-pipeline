"""Release tags (integration D17, D18): tag the target repo once every merged release is settled.

explorer marks the commit it verified when a batch settles (every finding resolved or dismissed)
with a GitHub commit status, state `success`, description `B-n settled: ...`. The context is
`explorer/batch-settled` (legacy) or, per module lane, `explorer/batch-settled/<lane>`; only the
latest status of each context on a commit counts, and one batch is settled on a commit when some
context of it succeeded and none failed (`settled_on`). With RELEASE_TAGS on, at most every
TAG_POLL_MIN the pipeline:

- fetches the base branch and the tags, and finds the newest `<TAG_PREFIX><digit>...` tag reachable
  from origin/<base> (none: it scans the last TAG_SCAN_COMMITS first-parent commits);
- reads the statuses of every first-parent commit after that tag, newest first; a commit with a
  settled batch is a candidate;
- lists the merged release PRs (label RELEASE_PR_LABEL); each whose body has a `Batch: B-n` line and
  whose merge commit is in that range is a batch release, and a candidate is tagged only when every
  batch release merged at or before it has a success for its own batch id on a commit from its
  merge up to the candidate (lanes are never compared). The newest passing candidate wins; with
  no batch release the newest candidate wins. The lane is shown: `B-4 (billing)`;
- names the tag after the local date (`v2026.11.24`, then `.2`, `.3` the same day), creates it
  annotated on that commit, pushes it, and creates a GitHub Release whose notes list the
  first-parent commits since the previous tag (PR number and title for a PR merge);
- posts one line in Slack.

When every candidate is blocked nothing is tagged; one `blocked by R-n (B-n, PR #n, merged <sha9>): ...`
line per release stopping the newest candidate is logged (and printed by `claude-pipeline tag
--dry-run`), never posted to Slack or alerted: a blocked tag is a normal waiting state.

A tag is never moved or deleted, and only a commit on the first-parent history of origin/<base> is
ever tagged. A failure is logged, alerted once per commit (the ledger remembers it), and retried on
the next poll; a tag that was created but not pushed or released is finished first on the next
pass (once it exists it is the previous tag, so the scan alone would never come back to it). Never raises into the daemon: every subprocess result is read by its returncode.
So a production deploy can follow tags later: a `v*` tag exists only on a verified, settled commit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .log import get
from .procenv import clean_env
from .releases import BATCH_LINE, IDENTITY

log = get("tagging")

CONTEXT = "explorer/batch-settled"
LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SETTLED_RE = re.compile(r"^(B-\d+) settled")
PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
WITH_TAG_CAP = 200          # commits read after the previous tag, at most (a safety bound)
ALERT_KEY = "tag_alert:{sha}"
PENDING_KEY = "tag_pending"       # a created tag not yet pushed or released (JSON plan)


@dataclass
class Plan:
    """What one pass would tag."""
    name: str
    sha: str
    batches: list[str]
    previous: str                     # the previous tag, or "" when there is none
    notes: str
    commits: list[str] = field(default_factory=list)   # the range read, newest first
    lanes: dict[str, str] = field(default_factory=dict)      # batch id -> lane, "" = legacy


@dataclass
class Outcome:
    plan: Plan | None
    tagged: bool = False
    url: str = ""
    error: str = ""
    blockers: list[str] = field(default_factory=list)        # one line per blocking release


def label(batch: str, lanes: dict[str, str]) -> str:
    """`B-4 (billing)` when the batch's lane is known, else `B-4`."""
    lane = lanes.get(batch)
    return f"{batch} ({lane})" if lane else batch


def labels(batches: list[str], lanes: dict[str, str]) -> str:
    return ", ".join(label(b, lanes) for b in batches)


def settled_batches(statuses: list[dict]) -> list[str]:
    """The batch ids of a commit's latest `explorer/batch-settled` status when it is a success.
    GitHub lists a commit's statuses newest first; only the latest one per context counts."""
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None)
    if not latest or latest.get("state") != "success":
        return []
    m = SETTLED_RE.match((latest.get("description") or "").strip())
    return [m.group(1)] if m else []


def settled_on(statuses: list[dict]) -> dict[str, str]:
    """{batch id: lane} for every batch settled on this commit. lane is "" for the legacy context.

    Only the latest status of each settled context counts. A batch is settled when at least one of
    its contexts' latest status is a success and none is a failure; its lane is that of a
    successful lane context (the first in sorted order), "" when only the legacy one succeeded."""
    latest: dict[str, tuple[str, dict]] = {}
    for st in statuses:
        ctx = st.get("context")
        if not isinstance(ctx, str) or ctx in latest:
            continue
        if ctx == CONTEXT:
            latest[ctx] = ("", st)
        elif ctx.startswith(CONTEXT + "/") and LANE_RE.fullmatch(ctx[len(CONTEXT) + 1:]):
            latest[ctx] = (ctx[len(CONTEXT) + 1:], st)
    states: dict[str, list[tuple[str, str]]] = {}          # batch -> [(lane, state)]
    for lane, st in latest.values():
        m = SETTLED_RE.match((st.get("description") or "").strip())
        if m:
            states.setdefault(m.group(1), []).append((lane, st.get("state")))
    out: dict[str, str] = {}
    for batch, rows in states.items():
        if any(state == "failure" for _, state in rows):
            continue
        won = sorted(lane for lane, state in rows if state == "success")
        if not won:
            continue
        out[batch] = next((ln for ln in won if ln), "")
    return out


def tag_name(prefix: str, day: dt.date, taken) -> str:
    """`<prefix>YYYY.MM.DD`, then `.2`, `.3`… while `taken(name)` says it exists."""
    base = f"{prefix}{day:%Y.%m.%d}"
    name, n = base, 1
    while taken(name):
        n += 1
        name = f"{base}.{n}"
    return name


def merged_line(subject: str, body: str) -> str:
    """One notes line for a first-parent commit: `#N title` for a PR merge, else the subject."""
    m = PR_MERGE_RE.match(subject.strip())
    if not m:
        return f"- {subject.strip()}"
    title = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
    return f"- #{m.group(1)} {title or subject.strip()}"


def render_notes(batches: list[str], sha: str, lines: list[str],
                 lanes: dict[str, str] | None = None) -> str:
    head = (f"{labels(batches, lanes or {})} settled on UAT at `{sha[:9]}` "
            f"(explorer `{CONTEXT}`: every finding resolved or dismissed).")
    return head + "\n\n## Merged\n" + ("\n".join(lines) if lines else "- (no commits since the previous tag)")


class Tagger:
    """One pass at a time; `run` is injected by the tests (it never reaches a network there)."""

    def __init__(self, cfg, *, slack=None, ledger=None, run=subprocess.run, today=None):
        self.cfg, self.slack, self.ledger, self._run = cfg, slack, ledger, run
        self._today = today or (lambda: dt.datetime.now().astimezone().date())

    # --- subprocesses ----------------------------------------------------------------------
    def _cmd(self, args: list[str], *, creds: bool, identity: bool = False,
             timeout: int = 120) -> subprocess.CompletedProcess:
        env = clean_env("github" if creds else "plain")
        if identity:
            env = {**env, **IDENTITY}
        try:
            return self._run(args, cwd=Path(self.cfg.repo_path), capture_output=True, text=True,
                             timeout=timeout, env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return subprocess.CompletedProcess(args, 1, "", str(e))

    def _git(self, *args: str, creds: bool = False, identity: bool = False) -> subprocess.CompletedProcess:
        return self._cmd(["git", *args], creds=creds, identity=identity)

    def _gh(self, *args: str) -> subprocess.CompletedProcess:
        return self._cmd(["gh", *args], creds=True)

    # --- reading -----------------------------------------------------------------------------
    def _fetch(self) -> str:
        base = self.cfg.base_branch
        cp = self._git("fetch", "--quiet", "--tags", "origin",
                       f"+refs/heads/{base}:refs/remotes/origin/{base}", creds=True)
        return "" if cp.returncode == 0 else f"git fetch failed: {(cp.stderr or '').strip()[:200]}"

    def _previous(self) -> str:
        cp = self._git("describe", "--tags", "--abbrev=0", "--match", f"{self.cfg.tag_prefix}[0-9]*",
                       f"origin/{self.cfg.base_branch}")
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def _range(self, previous: str) -> list[str]:
        head = f"origin/{self.cfg.base_branch}"
        if previous:
            args = ["rev-list", "--first-parent", f"--max-count={WITH_TAG_CAP}", head, f"^{previous}"]
        else:
            args = ["rev-list", "--first-parent", f"--max-count={max(1, self.cfg.tag_scan_commits)}", head]
        cp = self._git(*args)
        return cp.stdout.split() if cp.returncode == 0 else []

    def _statuses(self, sha: str) -> tuple[list[dict] | None, str]:
        """The commit's statuses, or None and why (gh's own message, so a token without the
        commit-statuses permission says so)."""
        cp = self._gh("api", f"repos/{{owner}}/{{repo}}/commits/{sha}/statuses?per_page=100")
        if cp.returncode != 0:
            return None, (cp.stderr or cp.stdout or "").strip()[:300]
        try:
            rows = json.loads(cp.stdout or "[]")
        except json.JSONDecodeError:
            return None, "gh returned something that is not JSON"
        return (rows, "") if isinstance(rows, list) else (None, "gh returned something that is not a list")

    def _release_prs(self) -> tuple[list[dict] | None, str]:
        """Merged release PRs, or None and why."""
        cp = self._gh("pr", "list", "--label", self.cfg.release_pr_label, "--state", "merged",
                      "--json", "number,headRefName,mergeCommit,body", "--limit", "100")
        if cp.returncode != 0:
            return None, f"could not list the release PRs: {(cp.stderr or cp.stdout or '').strip()[:300]}"
        try:
            rows = json.loads(cp.stdout or "[]")
        except json.JSONDecodeError:
            return None, "could not list the release PRs: gh returned something that is not JSON"
        if not isinstance(rows, list):
            return None, "could not list the release PRs: gh returned something that is not a list"
        return [r for r in rows if isinstance(r, dict)], ""

    def _tag_exists(self, name: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"refs/tags/{name}").returncode == 0

    def _tag_target(self, name: str) -> str:
        cp = self._git("rev-parse", "--verify", "--quiet", f"refs/tags/{name}^{{commit}}")
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def _log_lines(self, commits: list[str], sha: str) -> list[str]:
        """Notes lines for the range's commits from `sha` back (oldest last)."""
        out = []
        for c in commits[commits.index(sha):]:
            cp = self._git("show", "-s", "--format=%s%x1f%b", c)
            if cp.returncode != 0:
                continue
            subject, _, body = cp.stdout.partition("\x1f")
            out.append(merged_line(subject, body))
        return out

    def _batch_releases(self, index: dict[str, int]) -> tuple[list[dict] | None, str]:
        """The merged release PRs that name a batch and were merged inside the window (`index`:
        sha -> position, newest first), as {name, batch, number, m, merge}."""
        prs, why = self._release_prs()
        if prs is None:
            return None, why
        out = []
        for pr in prs:
            mc = pr.get("mergeCommit")
            merge = mc.get("oid") if isinstance(mc, dict) else None
            m = BATCH_LINE.search(pr.get("body") or "")
            if not merge or merge not in index or not m:
                continue
            head = str(pr.get("headRefName") or "")
            name = head[len("release/"):].upper() if head.startswith("release/") else head
            out.append({"name": name, "batch": m.group(1), "number": pr.get("number"),
                        "m": index[merge], "merge": merge})
        out.sort(key=lambda r: (r["m"], r["number"] or 0))
        return out, ""

    def plan(self) -> tuple[Plan | None, str, list[str]]:
        """(what to tag, error, blocker lines). (None, "", []) when nothing is settled since the
        previous tag; the blocker lines name each release that stops the newest settled commit."""
        err = self._fetch()
        if err:
            return None, err, []
        previous = self._previous()
        commits = self._range(previous)
        settled: list[dict[str, str]] = []                   # settled_on() of each commit, newest first
        for sha in commits:
            rows, why = self._statuses(sha)
            if rows is None:
                return None, f"could not read the statuses of {sha[:9]}: {why}", []
            settled.append(settled_on(rows))
        candidates = [i for i, s in enumerate(settled) if s]
        if not candidates:
            return None, "", []
        releases, why = self._batch_releases({sha: i for i, sha in enumerate(commits)})
        if releases is None:
            return None, why, []

        def uncovered(i: int) -> list[dict]:
            return [r for r in releases if r["m"] >= i
                    and not any(r["batch"] in settled[j] for j in range(i, r["m"] + 1))]

        blockers = [(f"blocked by {r['name']} ({r['batch']}, PR #{r['number']}, merged {r['merge'][:9]}): "
                     f"no successful {CONTEXT} status for {r['batch']} since its merge")
                    for r in uncovered(candidates[0])]
        i = next((c for c in candidates if not uncovered(c)), None)
        if i is None:
            return None, "", blockers
        chosen = commits[i]
        lanes: dict[str, str] = {}
        for found in settled[i:]:                            # newest first; a lane beats ""
            for b, lane in found.items():
                if not lanes.get(b):
                    lanes[b] = lane
        batches = sorted(lanes, key=lambda b: int(b.split("-")[1]))
        # reuse a tag of ours that was created but never pushed, else the next free name
        name = tag_name(self.cfg.tag_prefix, self._today(),
                        lambda n: self._tag_exists(n) and self._tag_target(n) != chosen)
        notes = render_notes(batches, chosen, self._log_lines(commits, chosen), lanes)
        return Plan(name, chosen, batches, previous, notes, commits, lanes), "", blockers

    # --- writing -----------------------------------------------------------------------------
    def _create(self, p: Plan) -> tuple[str, str, bool]:
        """(release url, error, whether the tag is on origin)."""
        if self._tag_target(p.name) != p.sha:
            msg = f"{labels(p.batches, p.lanes)} settled on UAT ({CONTEXT})"
            cp = self._git("tag", "-a", p.name, p.sha, "-m", msg, identity=True)
            if cp.returncode != 0:
                return "", f"git tag failed: {(cp.stderr or '').strip()[:200]}", False
        cp = self._git("push", "--quiet", "origin", f"refs/tags/{p.name}", creds=True)
        if cp.returncode != 0:
            return "", f"git push of {p.name} failed: {(cp.stderr or '').strip()[:200]}", False
        view = self._gh("release", "view", p.name, "--json", "url", "--jq", ".url")
        if view.returncode == 0 and view.stdout.strip():
            return view.stdout.strip(), "", True
        cp = self._gh("release", "create", p.name, "--verify-tag", "--title", p.name, "--notes", p.notes)
        if cp.returncode != 0:
            return "", f"gh release create {p.name} failed: {(cp.stderr or '').strip()[:200]}", True
        return cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "", "", True

    def _alert_once(self, sha: str, text: str) -> None:
        log.warning("release tag: %s", text)
        key = ALERT_KEY.format(sha=sha or "fetch")
        if self.ledger is not None and self.ledger.get_meta(key):
            return
        if self.slack is not None:
            self.slack.alert(f"release tag not created: {text} (retrying every {self.cfg.tag_poll_min} min)")
        if self.ledger is not None:
            self.ledger.set_meta(key, dt.datetime.now().isoformat(timespec="seconds"))

    def _pending(self) -> Plan | None:
        if self.ledger is None:
            return None
        raw = self.ledger.get_meta(PENDING_KEY) or ""
        try:
            d = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None
        return Plan(d["name"], d["sha"], d["batches"], d.get("previous", ""), d["notes"],
                    lanes=d.get("lanes") or {}) if d else None

    def _set_pending(self, p: Plan | None) -> None:
        if self.ledger is not None:
            self.ledger.set_meta(PENDING_KEY, json.dumps({"name": p.name, "sha": p.sha, "batches": p.batches,
                                                          "previous": p.previous, "notes": p.notes,
                                                          "lanes": p.lanes}) if p else "")

    def _publish(self, p: Plan) -> Outcome:
        try:
            url, err, pushed = self._create(p)
        except Exception as e:  # noqa: BLE001
            url, err, pushed = "", f"{type(e).__name__}: {e}", False
        if err:
            if pushed or self._tag_target(p.name) == p.sha:
                # The tag exists (here, or on origin too): it is now the newest tag, so the scan
                # alone would never come back to this commit. The next pass finishes it first.
                self._set_pending(p)
            self._alert_once(p.sha, err)
            return Outcome(p, error=err)
        self._set_pending(None)
        log.info("tagged %s on %s (%s)", p.name, p.sha[:9], ", ".join(p.batches))
        if self.slack is not None:
            self.slack.post(f":label: Tagged {p.name} on {p.sha[:9]}: {labels(p.batches, p.lanes)} settled"
                            + (f" · {url}" if url else ""))
        return Outcome(p, tagged=True, url=url)

    def run_pass(self, *, dry_run: bool = False) -> Outcome:
        """One pass. Never raises."""
        pending = None if dry_run else self._pending()
        if pending is not None:
            return self._publish(pending)
        try:
            p, err, blockers = self.plan()
        except Exception as e:  # noqa: BLE001 - never into the daemon
            p, err, blockers = None, f"{type(e).__name__}: {e}", []
        for line in blockers:
            log.info("release tag: %s", line)
        if err:
            if not dry_run:
                self._alert_once("", err)
            return Outcome(None, error=err)
        if p is None:
            return Outcome(None, blockers=blockers)
        if dry_run:
            return Outcome(p)
        return self._publish(p)


def describe(o: Outcome) -> str:
    """What `claude-pipeline tag` prints."""
    if o.error:
        return f"error: {o.error}"
    if o.plan is None and o.blockers:
        return ("Nothing to tag: every commit with a settled batch also contains a release that is not "
                "settled yet.\n" + "\n".join(o.blockers))
    if o.plan is None:
        return "Nothing to tag: no commit since the previous tag has a settled batch."
    p = o.plan
    head = (f"{'tagged' if o.tagged else 'would tag'} {p.name} on {p.sha}\n"
            f"batches: {', '.join(p.batches)}\nprevious tag: {p.previous or '(none)'}"
            + (f"\nrelease: {o.url}" if o.url else ""))
    return head + "\n\n" + p.notes
