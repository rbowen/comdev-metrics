"""CLI entry point for asfmetrics."""

import json
import argparse
import sys
from pathlib import Path

from asfmetrics.config import load_config
from asfmetrics.collectors.mailing_lists import (
    collect_mailing_list_stats,
    discover_all_active_projects,
    invalidate_cache,
)
from asfmetrics.collectors.git_activity import (
    collect_git_activity,
    invalidate_git_cache,
    get_rate_limit_status,
)
from asfmetrics.collectors.github_repos import collect_repo_inventory
from asfmetrics.collectors.projects_apache_org import (
    collect_projects_apache_org,
    extract_active_projects,
)
from asfmetrics.output.json_api import write_json


def status(msg: str):
    """Print a single-line status that overwrites itself."""
    sys.stdout.write(f"\r\033[K  {msg}")
    sys.stdout.flush()


def status_done(msg: str):
    """Print a final status line (stays visible)."""
    sys.stdout.write(f"\r\033[K  {msg}\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(
        prog="asfmetrics",
        description="Collect and publish Apache project community metrics.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config.yml (default: auto-discover)",
    )
    parser.add_argument(
        "--project", type=str, default=None,
        help="Run for a single project only (overrides config)",
    )
    parser.add_argument(
        "--skip-mailing-lists", action="store_true",
        help="Skip mailing list collection (faster for testing)",
    )
    parser.add_argument(
        "--skip-git", action="store_true",
        help="Skip git/VCS activity collection",
    )
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="Ignore cached data and re-fetch everything",
    )
    parser.add_argument(
        "--refresh-repos", action="store_true",
        help="Re-fetch the GitHub repo inventory (project → repos map)",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    print("asfmetrics: loaded config")

    # Handle --force-refresh: clear caches
    if args.force_refresh:
        invalidate_cache(config)
        invalidate_git_cache(config)
        print("  caches cleared (--force-refresh)")

    # Repo inventory: auto-run if _project_map.json is missing,
    # or if --refresh-repos / --force-refresh is specified.
    json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
    project_map_path = json_dir / "_project_map.json"
    need_inventory = (
        args.refresh_repos
        or args.force_refresh
        or not project_map_path.exists()
    )
    if need_inventory and not args.skip_git:
        status("fetching GitHub repo inventory (apache org)...")
        inv = collect_repo_inventory(config)
        status_done(
            f"repo inventory: {inv['repo_count']} repos → {inv['project_count']} projects"
        )

    # Phase 1: Fetch foundation-wide data from projects.apache.org
    status("fetching foundation data from projects.apache.org...")
    pao_result = collect_projects_apache_org(config)
    write_json("_foundation", pao_result["roster_changes"], config)
    status_done(f"foundation data: {len(pao_result['active_projects'])} active projects")

    # Determine project list
    projects = config.get("projects", ["all"])
    if args.project:
        projects = [args.project]

    if "all" in projects:
        projects = pao_result["active_projects"]

    # Phase 2: Per-project mailing list collection
    if not args.skip_mailing_lists:
        total = len(projects)
        collected = 0
        skipped = 0

        for i, project in enumerate(projects, 1):
            status(f"[{i}/{total}] {project}: fetching mailing lists...")
            stats = collect_mailing_list_stats(project, config)
            if stats:
                active_count = len(stats["active_lists"])
                total_msgs = sum(l["messages"] for l in stats["active_lists"])
                write_json(project, stats, config)
                collected += 1
                status(f"[{i}/{total}] {project}: {active_count} active lists, {total_msgs} messages")
            else:
                skipped += 1
                status(f"[{i}/{total}] {project}: no active lists")

        status_done(f"mailing lists: {collected} projects collected, {skipped} skipped (no activity)")

        # Write mailing_summary.json for the overview page (avoids 200+ fetches)
        json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
        summary = []
        for project_name in projects:
            project_file = json_dir / f"{project_name}.json"
            if project_file.exists():
                try:
                    with open(project_file) as f:
                        data = json.load(f)
                    for lst in data.get("active_lists", []):
                        summary.append({
                            "project": data.get("project", project_name),
                            "list_name": lst.get("list_name", ""),
                            "messages": lst.get("messages", 0),
                            "active_months": lst.get("active_months", {}),
                        })
                except (json.JSONDecodeError, OSError):
                    pass
        summary_path = json_dir / "_cache" / "mailing_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w") as f:
            json.dump(summary, f)
        status_done(f"mailing summary: {len(summary)} lists written to _cache/mailing_summary.json")
    else:
        print("  skipping mailing list collection (--skip-mailing-lists)")

    # Phase 3: Per-project git/VCS activity collection
    if not args.skip_git:
        total = len(projects)
        collected = 0
        skipped = 0
        errors = 0

        for i, project in enumerate(projects, 1):
            status(f"[{i}/{total}] {project}: fetching git activity...")
            try:
                git_stats = collect_git_activity(project, config)
                if git_stats and git_stats.get("totals", {}).get("commits", 0) > 0:
                    write_json(f"{project}_git", git_stats, config)
                    commits = git_stats["totals"]["commits"]
                    committers = git_stats["totals"]["committers"]
                    status(f"[{i}/{total}] {project}: {commits} commits, {committers} committers")
                    collected += 1
                else:
                    skipped += 1
                    status(f"[{i}/{total}] {project}: no git activity")
            except Exception as e:
                errors += 1
                status(f"[{i}/{total}] {project}: error - {e}")

            # Print budget every 20 projects
            if i % 20 == 0:
                rl = get_rate_limit_status()
                if rl["remaining"] is not None:
                    status_done(f"  [API budget: {rl['remaining']}/{rl['limit']} remaining, {rl['used']} calls used]")

        status_done(f"git activity: {collected} collected, {skipped} skipped, {errors} errors")
        # Show rate limit status
        rl = get_rate_limit_status()
        if rl["remaining"] is not None:
            print(f"  GitHub API budget: {rl['remaining']}/{rl['limit']} remaining "
                  f"({rl['used']} calls this session)")
    else:
        print("  skipping git/VCS collection (--skip-git)")

    print("\ndone.")


if __name__ == "__main__":
    main()
