"""CLI entrypoint: `claude-pipeline <command>`.

Commands:
  run          Start the dispatcher daemon (poll → claim → work → gate → PR).
  selftest     Verify config, Vikunja connectivity, buckets, ntree, gh.
  cleanup      Run one PR-close cleanup pass and exit.
  nightly      Run the nightly pass (ntree doctor + report) and exit.
  prepare-repo Drop worker-settings.json + CLAUDE.md template into the target repo.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config as cfgmod
from . import log as logmod
from .cleanup import nightly as run_nightly
from .cleanup import process_closed_prs
from .notify import Slack
from .ntree import Ntree

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _load(env_file: str):
    cfg = cfgmod.load(env_file)
    logmod.setup(cfg.log_level)
    return cfg


def cmd_run(args) -> int:
    from .daemon import Dispatcher

    cfg = _load(args.env)
    problems = cfgmod.validate(cfg)
    if problems:
        print("Configuration problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    Dispatcher(cfg).run()
    return 0


def cmd_selftest(args) -> int:
    cfg = _load(args.env)
    log = logmod.get("selftest")
    ok = True

    problems = cfgmod.validate(cfg)
    for p in problems:
        log.error("config: %s", p)
        ok = False
    if problems:
        return 2

    # Vikunja
    try:
        from .vikunja import Vikunja

        vk = Vikunja(cfg.vikunja_url, cfg.vikunja_token)
        view = vk.kanban_view_id(cfg.project_id)
        bmap = vk.bucket_map(cfg.project_id, view)
        log.info("Vikunja OK: kanban view=%s buckets=%s", view, sorted(bmap))
        for key, title in cfg.bucket_names.items():
            if title.strip().lower() not in bmap:
                log.error("missing bucket '%s' (%s)", title, key)
                ok = False
        label_id = vk.ensure_label(cfg.claude_label)
        log.info("label '%s' id=%s", cfg.claude_label, label_id)
    except Exception as e:  # noqa: BLE001
        log.error("Vikunja check failed: %s", e)
        ok = False

    # ntree + gh
    for tool in ("ntree", "gh", "claude", "git"):
        if shutil.which(tool) is None:
            log.error("missing tool on PATH: %s", tool)
            ok = False
        else:
            log.info("found %s", tool)

    # target repo readiness
    settings = cfg.repo_path / cfg.settings_file
    if not settings.exists():
        log.warning("target repo missing %s (run `prepare-repo`)", cfg.settings_file)
    claude_md = cfg.repo_path / "CLAUDE.md"
    if not claude_md.exists():
        log.warning("target repo missing CLAUDE.md (spec: repo not eligible without it)")

    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_cleanup(args) -> int:
    cfg = _load(args.env)
    from .daemon import Dispatcher

    d = Dispatcher(cfg)
    d.refresh_board()
    process_closed_prs(cfg, d.ledger, d.ntree, d.vk, d.buckets, d.slack)
    return 0


def cmd_nightly(args) -> int:
    cfg = _load(args.env)
    ntree = Ntree(cfg.repo_path, cfg.base_branch)
    slack = Slack(cfg.slack_webhook)
    summary = run_nightly(cfg, ntree, slack)
    slack.nightly(summary)
    return 0


def cmd_prepare_repo(args) -> int:
    cfg = _load(args.env)
    log = logmod.get("prepare-repo")
    dest_settings = cfg.repo_path / ".claude" / "worker-settings.json"
    dest_settings.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES / "worker-settings.json", dest_settings)
    log.info("wrote %s", dest_settings)

    dest_claude = cfg.repo_path / "CLAUDE.md"
    if dest_claude.exists() and not args.force:
        log.warning("CLAUDE.md exists; not overwriting (use --force). Template at %s",
                    TEMPLATES / "CLAUDE.md.template")
    else:
        shutil.copyfile(TEMPLATES / "CLAUDE.md.template", dest_claude)
        log.info("wrote %s (edit it before enabling the repo)", dest_claude)
    print("Prepared. Review the two files, then run `claude-pipeline selftest`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-pipeline")
    p.add_argument("--env", default=".env", help="path to env file (default: .env)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="start the dispatcher daemon").set_defaults(func=cmd_run)
    sub.add_parser("selftest", help="verify config and connectivity").set_defaults(func=cmd_selftest)
    sub.add_parser("cleanup", help="run one PR-close cleanup pass").set_defaults(func=cmd_cleanup)
    sub.add_parser("nightly", help="run the nightly maintenance pass").set_defaults(func=cmd_nightly)
    pr = sub.add_parser("prepare-repo", help="install worker-settings.json + CLAUDE.md into target repo")
    pr.add_argument("--force", action="store_true", help="overwrite an existing CLAUDE.md")
    pr.set_defaults(func=cmd_prepare_repo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
