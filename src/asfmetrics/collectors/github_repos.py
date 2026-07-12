"""GitHub repository discovery for the Apache org.

On each run, fetches the full list of repos under github.com/apache
and classifies them into projects, detecting:
- New repos (not seen in previous run)
- Renamed/redirected repos (incubator- prefix removed on graduation)
- Archived repos (retired projects)

Repo naming conventions:
  apache/incubator-xyz-abc  → Incubating project "xyz", sub-repo "abc"
  apache/incubator-xyz      → Incubating project "xyz", main repo
  apache/foo-bar            → TLP "foo", sub-repo "bar"
  apache/foo                → TLP "foo", main repo
"""

import json
import os
import time
from pathlib import Path

import httpx


GITHUB_API = "https://api.github.com"
ORG = "apache"

# Secrets file locations to check for GITHUB_TOKEN
SECRETS_FILES = [
    Path(".secrets"),             # project-local (preferred for cron / portability)
    Path.home() / ".secrets",    # user-level fallback
    Path.home() / ".github_token",  # legacy fallback
]


# ---------------------------------------------------------------------------
# Rate-limit tracking (shared state for this module)
# ---------------------------------------------------------------------------

RATE_LIMIT_FLOOR = 50

_gh_rate = {
    "limit": None,
    "remaining": None,
    "reset_at": None,
    "used": 0,
}


def _update_gh_rate_limit(resp) -> None:
    """Update rate limit state from GitHub response headers."""
    remaining = resp.headers.get("x-ratelimit-remaining")
    limit = resp.headers.get("x-ratelimit-limit")
    reset_at = resp.headers.get("x-ratelimit-reset")
    if remaining is not None:
        _gh_rate["remaining"] = int(remaining)
    if limit is not None:
        _gh_rate["limit"] = int(limit)
    if reset_at is not None:
        _gh_rate["reset_at"] = int(reset_at)
    _gh_rate["used"] += 1


def _check_gh_rate_limit() -> None:
    """Pause if approaching rate limit."""
    from datetime import datetime
    remaining = _gh_rate["remaining"]
    if remaining is None:
        return
    if remaining <= RATE_LIMIT_FLOOR:
        reset_at = _gh_rate["reset_at"]
        if reset_at:
            now = int(time.time())
            wait_seconds = max(reset_at - now + 5, 0)
            reset_time = datetime.fromtimestamp(reset_at).strftime("%H:%M:%S")
            print(f"\n{'='*60}")
            print(f"  ⚠️  GITHUB API RATE LIMIT WARNING (repo inventory)")
            print(f"  Remaining: {remaining}/{_gh_rate['limit']} | Resets at: {reset_time} ({wait_seconds}s)")
            print(f"  PAUSING until reset...")
            print(f"{'='*60}\n")
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _gh_rate["remaining"] = _gh_rate["limit"]
            print(f"  ✓ Rate limit reset. Resuming.\n")


def resolve_github_token(config: dict) -> str | None:
    """Resolve GitHub API token from multiple sources.

    Lookup order:
    1. GITHUB_TOKEN environment variable
    2. .secrets files (project-local ./.secrets first, then ~/.secrets)
       Format: GITHUB_TOKEN=ghp_...
    3. config.yml data_sources.git.token field

    Returns:
        Token string, or None if not found anywhere.
    """
    # 1. Environment variable
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()

    # 2. Secrets files
    for secrets_path in SECRETS_FILES:
        if secrets_path.exists():
            for line in secrets_path.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip()

    # 3. Config file
    return config.get("data_sources", {}).get("git", {}).get("token")


# Repos that don't follow project naming conventions
SKIP_REPOS = {
    ".github",
    "infrastructure-puppet",
    "infrastructure-actions",
}


def classify_repo(repo_name: str) -> dict:
    """Classify a repo name into project + sub-repo.

    Returns:
        dict with keys: project, sub_repo (or None), incubating (bool)
    """
    if repo_name.startswith("incubator-"):
        remainder = repo_name[len("incubator-"):]
        parts = remainder.split("-", 1)
        return {
            "project": parts[0],
            "sub_repo": parts[1] if len(parts) > 1 else None,
            "incubating": True,
        }
    else:
        parts = repo_name.split("-", 1)
        return {
            "project": parts[0],
            "sub_repo": parts[1] if len(parts) > 1 else None,
            "incubating": False,
        }


def fetch_all_repos(token: str | None = None) -> list[dict]:
    """Fetch all repos in the Apache GitHub org.

    Paginates through the full list (3000+ repos).

    Args:
        token: Optional GitHub personal access token for higher rate limits.

    Returns:
        List of repo dicts with name, archived, fork, description.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repos = []
    page = 1
    per_page = 100

    while True:
        url = f"{GITHUB_API}/orgs/{ORG}/repos"
        params = {"per_page": per_page, "page": page, "type": "sources"}
        _check_gh_rate_limit()

        resp = httpx.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        _update_gh_rate_limit(resp)
        batch = resp.json()

        if not batch:
            break

        for r in batch:
            if r["name"] in SKIP_REPOS:
                continue
            repos.append({
                "name": r["name"],
                "archived": r["archived"],
                "fork": r["fork"],
                "description": r.get("description") or "",
                "default_branch": r.get("default_branch", "main"),
            })

        page += 1

    return repos


def build_project_map(repos: list[dict]) -> dict:
    """Group repos by project with classification metadata.

    Returns:
        Dict keyed by project name, value is dict with:
          - repos: list of {name, sub_repo, archived, ...}
          - incubating: bool (True if ANY repo still has incubator- prefix)
          - archived: bool (True if ALL repos are archived)
    """
    projects = {}

    for repo in repos:
        if repo["fork"]:
            continue

        info = classify_repo(repo["name"])
        project_name = info["project"]

        if project_name not in projects:
            projects[project_name] = {
                "repos": [],
                "incubating": False,
                "archived": True,  # will be set False if any repo is active
            }

        projects[project_name]["repos"].append({
            "name": repo["name"],
            "sub_repo": info["sub_repo"],
            "archived": repo["archived"],
            "default_branch": repo["default_branch"],
        })

        if info["incubating"]:
            projects[project_name]["incubating"] = True
        if not repo["archived"]:
            projects[project_name]["archived"] = False

    return projects


def detect_changes(current: dict, previous_path: Path) -> dict:
    """Compare current project map against previous run.

    Args:
        current: Current project map from build_project_map().
        previous_path: Path to previously saved project_map.json.

    Returns:
        Dict with lists: new_projects, graduated, newly_archived, new_repos.
    """
    changes = {
        "new_projects": [],
        "graduated": [],       # incubating -> TLP (incubator- prefix removed)
        "newly_archived": [],  # active -> all repos archived
        "new_repos": [],       # new repos in existing projects
    }

    if not previous_path.exists():
        # First run — everything is "new"
        changes["new_projects"] = list(current.keys())
        return changes

    with open(previous_path) as f:
        previous = json.load(f)

    prev_names = set(previous.keys())
    curr_names = set(current.keys())

    changes["new_projects"] = sorted(curr_names - prev_names)

    for name in curr_names & prev_names:
        prev = previous[name]
        curr = current[name]

        # Graduated: was incubating, no longer
        if prev.get("incubating") and not curr.get("incubating"):
            changes["graduated"].append(name)

        # Newly archived: was active, now all repos archived
        if not prev.get("archived") and curr.get("archived"):
            changes["newly_archived"].append(name)

        # New repos added to existing project
        prev_repo_names = {r["name"] for r in prev.get("repos", [])}
        curr_repo_names = {r["name"] for r in curr.get("repos", [])}
        new_repos = curr_repo_names - prev_repo_names
        if new_repos:
            changes["new_repos"].extend(
                {"project": name, "repo": r} for r in sorted(new_repos)
            )

    return changes


def collect_repo_inventory(config: dict) -> dict:
    """Main entry point: fetch repos, classify, detect changes, save state.

    Args:
        config: Full asfmetrics config dict.

    Returns:
        Dict with project_map, changes, and repo_count.
    """
    token = resolve_github_token(config)
    json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
    json_dir.mkdir(parents=True, exist_ok=True)

    state_path = json_dir / "_project_map.json"

    print("    fetching Apache GitHub org repos...")
    repos = fetch_all_repos(token)
    print(f"    found {len(repos)} repos (excluding forks & skipped)")

    project_map = build_project_map(repos)
    print(f"    classified into {len(project_map)} projects")

    changes = detect_changes(project_map, state_path)

    # Save current state for next run
    with open(state_path, "w") as f:
        json.dump(project_map, f, indent=2)

    return {
        "repo_count": len(repos),
        "project_count": len(project_map),
        "project_map": project_map,
        "changes": changes,
    }
