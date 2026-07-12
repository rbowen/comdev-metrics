"""Git/VCS activity collector for ASF projects.

Collects per-project commit and contribution metrics over a 12-month window.
Supports multiple backends:
  - github: Uses GitHub API (zero-checkout, API-only)
  - svn: Uses svn log against ASF's Subversion repository

Backend selection:
  1. Per-project override in config.yml:
       project_overrides:
         httpd:
           vcs: svn
           svn_path: https://svn.apache.org/repos/asf/httpd/httpd/trunk
  2. Default backend from config (data_sources.git.backend, default: github)

Metrics collected (monthly time series):
  - commits: number of commits
  - committers: unique committer count
  - For GitHub additionally:
    - prs_opened: PRs opened
    - prs_merged: PRs merged
    - prs_closed: PRs closed without merge

Caching:
  - Past months are immutable. Once a month ends, git history for that month
    cannot change. We cache per-project monthly data and only re-fetch the
    current (in-progress) month.
  - Cache stored in: <json_dir>/_cache/git/<project>.json
  - --force-refresh clears all caches.
"""

import json
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from asfmetrics.collectors.github_repos import resolve_github_token


GITHUB_API = "https://api.github.com"
ASF_SVN_BASE = "https://svn.apache.org/repos/asf"

# ---------------------------------------------------------------------------
# GitHub API rate-limit tracking
# ---------------------------------------------------------------------------

# Threshold: if remaining calls drops below this, pause and wait for reset
RATE_LIMIT_FLOOR = 50

# Global state for rate limit tracking
_rate_limit = {
    "limit": None,       # Total budget (e.g. 5000)
    "remaining": None,   # Calls left
    "reset_at": None,    # Unix timestamp when budget resets
    "used": 0,           # Calls used this session
}


def _update_rate_limit(resp: httpx.Response) -> None:
    """Update rate limit state from GitHub response headers."""
    limit = resp.headers.get("x-ratelimit-limit")
    remaining = resp.headers.get("x-ratelimit-remaining")
    reset_at = resp.headers.get("x-ratelimit-reset")

    if remaining is not None:
        _rate_limit["remaining"] = int(remaining)
    if limit is not None:
        _rate_limit["limit"] = int(limit)
    if reset_at is not None:
        _rate_limit["reset_at"] = int(reset_at)
    _rate_limit["used"] += 1


def _check_rate_limit() -> None:
    """Check if we're approaching the rate limit. If so, wait for reset.

    Prints a loud warning and sleeps until the reset window.
    """
    remaining = _rate_limit["remaining"]
    if remaining is None:
        return  # Haven't made a request yet

    if remaining <= RATE_LIMIT_FLOOR:
        reset_at = _rate_limit["reset_at"]
        if reset_at:
            now = int(time.time())
            wait_seconds = max(reset_at - now + 5, 0)  # +5s buffer
            reset_time = datetime.fromtimestamp(reset_at).strftime("%H:%M:%S")
            print(f"\n{'='*60}")
            print(f"  ⚠️  GITHUB API RATE LIMIT WARNING")
            print(f"  Remaining: {remaining}/{_rate_limit['limit']}")
            print(f"  Used this session: {_rate_limit['used']}")
            print(f"  Resets at: {reset_time} ({wait_seconds}s from now)")
            print(f"  PAUSING until reset...")
            print(f"{'='*60}\n")
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            # After sleeping, optimistically reset our counter
            _rate_limit["remaining"] = _rate_limit["limit"]
            print(f"  ✓ Rate limit reset. Resuming.\n")
        else:
            print(f"\n  ⚠️  Rate limit low ({remaining} remaining) but no reset time known. Continuing cautiously.\n")


def get_rate_limit_status() -> dict:
    """Return current rate limit status (for external callers / CLI)."""
    return dict(_rate_limit)


# Default paths in ASF SVN for common projects
# Projects can override with project_overrides in config
SVN_PATH_CONVENTIONS = {
    "httpd": f"{ASF_SVN_BASE}/httpd/httpd/trunk",
    "subversion": f"{ASF_SVN_BASE}/subversion/trunk",
    "apr": f"{ASF_SVN_BASE}/apr/apr/trunk",
}


def _current_month_str() -> str:
    """Return current month as YYYY-MM."""
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"


def _twelve_months_ago() -> datetime:
    """Return datetime 12 months before now."""
    now = datetime.now()
    return now.replace(year=now.year - 1)


def _twelve_months_ago_str() -> str:
    """Return YYYY-MM for 12 months ago."""
    dt = _twelve_months_ago()
    return f"{dt.year}-{dt.month:02d}"


def _month_key(dt: datetime) -> str:
    """Format a datetime as YYYY-MM."""
    return dt.strftime("%Y-%m")


def _first_of_month(month_str: str) -> datetime:
    """Parse YYYY-MM into a datetime at the 1st of that month."""
    return datetime.strptime(month_str + "-01", "%Y-%m-%d")


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _cache_dir(config: dict) -> Path:
    """Return the git activity cache directory."""
    json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
    cache_dir = json_dir / "_cache" / "git"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _load_cache(project: str, config: dict) -> dict | None:
    """Load cached git activity data for a project."""
    cache_path = _cache_dir(config) / f"{project}.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(project: str, cache_data: dict, config: dict) -> None:
    """Save git activity cache for a project."""
    cache_path = _cache_dir(config) / f"{project}.json"
    with open(cache_path, "w") as f:
        json.dump(cache_data, f, indent=2)


def _cache_is_current(cache: dict) -> bool:
    """Check if cache was already fetched this month."""
    fetched_at = cache.get("_fetched_at", "")
    return fetched_at[:7] == _current_month_str()


def invalidate_git_cache(config: dict) -> None:
    """Remove all git activity cache files (for --force-refresh)."""
    cache_dir = _cache_dir(config)
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            f.unlink()
        print(f"    cleared {cache_dir}")


# ---------------------------------------------------------------------------
# GitHub backend
# ---------------------------------------------------------------------------

def _github_headers(token: str | None) -> dict:
    """Build GitHub API request headers."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_github_commits(
    org: str,
    repo: str,
    since: datetime,
    token: str | None = None,
) -> list[dict]:
    """Fetch commit activity for a repo from GitHub API.

    Returns list of {date, author} dicts.
    """
    headers = _github_headers(token)
    commits = []
    page = 1
    since_str = since.isoformat() + "Z"

    while True:
        url = f"{GITHUB_API}/repos/{org}/{repo}/commits"
        params = {"since": since_str, "per_page": 100, "page": page}
        _check_rate_limit()

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 409:
                # Empty repository
                break
            resp.raise_for_status()
            _update_rate_limit(resp)
            batch = resp.json()
        except (httpx.HTTPError, ValueError):
            break

        if not batch:
            break

        for c in batch:
            commit_date = None
            if c.get("commit", {}).get("author", {}).get("date"):
                try:
                    commit_date = datetime.fromisoformat(
                        c["commit"]["author"]["date"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            author = (
                c.get("author", {}).get("login")
                or c.get("commit", {}).get("author", {}).get("name", "unknown")
            )

            if commit_date:
                commits.append({"date": commit_date, "author": author})

        page += 1

        # Safety: don't paginate forever
        if page > 50:
            break

    return commits


def _fetch_github_prs(
    org: str,
    repo: str,
    since: datetime,
    token: str | None = None,
) -> list[dict]:
    """Fetch PR activity for a repo from GitHub API.

    Returns list of {created_at, merged_at, closed_at, state} dicts.
    """
    headers = _github_headers(token)
    prs = []
    page = 1
    since_str = since.isoformat() + "Z"

    while True:
        url = f"{GITHUB_API}/repos/{org}/{repo}/pulls"
        params = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
            "page": page,
        }
        _check_rate_limit()

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            _update_rate_limit(resp)
            batch = resp.json()
        except (httpx.HTTPError, ValueError):
            break

        if not batch:
            break

        # Stop paginating once we see PRs older than our window
        all_too_old = True
        for pr in batch:
            created = pr.get("created_at", "")
            if created < since_str:
                continue
            all_too_old = False

            prs.append({
                "created_at": created,
                "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"),
                "state": pr.get("state"),
            })

        if all_too_old:
            break

        page += 1
        if page > 30:
            break

    return prs


def _aggregate_github_monthly(
    repos: list[str],
    org: str,
    since: datetime,
    token: str | None,
) -> dict:
    """Fetch GitHub data per-repo into monthly buckets.

    Returns dict of {repo_name: {month_str: {commits, committers, committer_list, prs_opened, prs_merged, prs_closed}}}
    """
    per_repo = {}

    for repo in repos:
        monthly_commits = defaultdict(int)
        monthly_committers = defaultdict(set)
        monthly_prs_opened = defaultdict(int)
        monthly_prs_merged = defaultdict(int)
        monthly_prs_closed = defaultdict(int)

        # Commits
        commits = _fetch_github_commits(org, repo, since, token)
        for c in commits:
            month = _month_key(c["date"])
            monthly_commits[month] += 1
            monthly_committers[month].add(c["author"])

        # PRs
        prs = _fetch_github_prs(org, repo, since, token)
        for pr in prs:
            created_month = pr["created_at"][:7]  # "YYYY-MM"
            monthly_prs_opened[created_month] += 1

            if pr.get("merged_at"):
                merged_month = pr["merged_at"][:7]
                monthly_prs_merged[merged_month] += 1
            elif pr["state"] == "closed":
                closed_month = (pr.get("closed_at") or pr["created_at"])[:7]
                monthly_prs_closed[closed_month] += 1

        all_months = sorted(
            set(monthly_commits.keys())
            | set(monthly_prs_opened.keys())
        )

        if all_months:
            repo_data = {}
            for month in all_months:
                repo_data[month] = {
                    "commits": monthly_commits[month],
                    "committers": len(monthly_committers[month]),
                    "committer_list": sorted(monthly_committers[month]),
                    "prs_opened": monthly_prs_opened[month],
                    "prs_merged": monthly_prs_merged[month],
                    "prs_closed": monthly_prs_closed[month],
                }
            per_repo[repo] = repo_data

    return per_repo



def collect_github_activity(
    project: str,
    repos: list[str],
    org: str,
    token: str | None,
    config: dict,
) -> dict:
    """Collect Git activity metrics per-repo for a project via GitHub API, with caching.

    Output format: list of repos each with their own monthly time-series
    (mirrors mailing list structure for the frontend).
    """
    current_month = _current_month_str()
    cache = _load_cache(project, config)

    if cache and _cache_is_current(cache) and "repos_data" in cache:
        return _build_result_from_cache(project, "github", cache, org=org, repos=repos)

    if cache and cache.get("repos_data"):
        # Stale cache — only fetch current month for each repo
        since = _first_of_month(current_month)
        fresh = _aggregate_github_monthly(repos, org, since, token)

        cached_repos = cache.get("repos_data", {})
        for repo_name, fresh_months in fresh.items():
            if repo_name not in cached_repos:
                cached_repos[repo_name] = {}
            if current_month in fresh_months:
                cached_repos[repo_name][current_month] = fresh_months[current_month]

        cache["repos_data"] = cached_repos
        cache["_fetched_at"] = datetime.now().strftime("%Y-%m-%d")
        _save_cache(project, cache, config)
        return _build_result_from_cache(project, "github", cache, org=org, repos=repos)

    # No cache — full 12-month fetch
    since = _twelve_months_ago()
    repos_data = _aggregate_github_monthly(repos, org, since, token)

    cache_data = {
        "_fetched_at": datetime.now().strftime("%Y-%m-%d"),
        "_project": project,
        "_vcs": "github",
        "_org": org,
        "_repos": repos,
        "repos_data": repos_data,
    }
    _save_cache(project, cache_data, config)
    return _build_result_from_cache(project, "github", cache_data, org=org, repos=repos)


# ---------------------------------------------------------------------------
# SVN backend
# ---------------------------------------------------------------------------

def _parse_svn_log_xml(xml_text: str) -> list[dict]:
    """Parse `svn log --xml` output into a list of {date, author} dicts."""
    import re

    entries = []
    for entry_match in re.finditer(
        r'<logentry[^>]*>(.*?)</logentry>', xml_text, re.DOTALL
    ):
        block = entry_match.group(1)
        author_m = re.search(r'<author>(.*?)</author>', block)
        date_m = re.search(r'<date>(.*?)</date>', block)

        if date_m:
            try:
                dt = datetime.fromisoformat(
                    date_m.group(1).replace("Z", "+00:00")
                )
                author = author_m.group(1) if author_m else "unknown"
                entries.append({"date": dt, "author": author})
            except (ValueError, TypeError):
                pass

    return entries


def _aggregate_svn_monthly(svn_url: str, since: datetime, project: str) -> dict:
    """Fetch SVN log and return per-repo monthly data.

    Returns dict of {repo_name: {month_str: {commits, committers, committer_list}}}
    (SVN has only one "repo" — the trunk URL — but we use the same structure)
    """
    since_str = since.strftime("%Y-%m-%d")
    cmd = [
        "svn", "log", "--xml", "-q",
        "-r", f"{{{since_str}}}:HEAD",
        svn_url,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            print(f"    warning: svn log failed for {project}: {result.stderr[:200]}")
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"    warning: svn log failed for {project}: {e}")
        return {}

    entries = _parse_svn_log_xml(result.stdout)

    monthly_commits = defaultdict(int)
    monthly_committers = defaultdict(set)

    for entry in entries:
        month = _month_key(entry["date"])
        monthly_commits[month] += 1
        monthly_committers[month].add(entry["author"])

    all_months = sorted(monthly_commits.keys())
    monthly_data = {}
    for month in all_months:
        monthly_data[month] = {
            "commits": monthly_commits[month],
            "committers": len(monthly_committers[month]),
            "committer_list": sorted(monthly_committers[month]),
        }

    # Use the last path segment of the URL as the "repo name"
    repo_name = svn_url.rstrip("/").rsplit("/", 1)[-1]
    return {repo_name: monthly_data} if monthly_data else {}


def collect_svn_activity(
    project: str,
    svn_url: str,
    config: dict,
) -> dict:
    """Collect commit activity from SVN, with caching. Same per-repo structure."""
    current_month = _current_month_str()
    cache = _load_cache(project, config)

    if cache and _cache_is_current(cache) and "repos_data" in cache:
        return _build_result_from_cache(project, "svn", cache, svn_url=svn_url)

    if cache and cache.get("repos_data"):
        since = _first_of_month(current_month)
        fresh = _aggregate_svn_monthly(svn_url, since, project)

        cached_repos = cache.get("repos_data", {})
        for repo_name, fresh_months in fresh.items():
            if repo_name not in cached_repos:
                cached_repos[repo_name] = {}
            if current_month in fresh_months:
                cached_repos[repo_name][current_month] = fresh_months[current_month]

        cache["repos_data"] = cached_repos
        cache["_fetched_at"] = datetime.now().strftime("%Y-%m-%d")
        _save_cache(project, cache, config)
        return _build_result_from_cache(project, "svn", cache, svn_url=svn_url)

    # No cache — full fetch
    since = _twelve_months_ago()
    repos_data = _aggregate_svn_monthly(svn_url, since, project)

    cache_data = {
        "_fetched_at": datetime.now().strftime("%Y-%m-%d"),
        "_project": project,
        "_vcs": "svn",
        "_svn_url": svn_url,
        "repos_data": repos_data,
    }
    _save_cache(project, cache_data, config)
    return _build_result_from_cache(project, "svn", cache_data, svn_url=svn_url)


# ---------------------------------------------------------------------------
# Result builder (from cache)
# ---------------------------------------------------------------------------

def _build_result_from_cache(
    project: str,
    vcs: str,
    cache: dict,
    org: str = None,
    repos: list[str] = None,
    svn_url: str = None,
) -> dict:
    """Build the output dict from cached per-repo monthly data.

    Output format (mirrors mailing list structure):
    {
        project, vcs, org/svn_url,
        active_repos: [
            {repo_name, commits, committers, monthly: {month: {commits, committers, ...}}}
        ],
        totals: {commits, committers, prs_opened, prs_merged}
    }
    """
    cutoff = _twelve_months_ago_str()
    repos_data = cache.get("repos_data", {})

    active_repos = []
    all_committers = set()
    total_commits = 0
    total_prs_opened = 0
    total_prs_merged = 0

    for repo_name, monthly in sorted(repos_data.items()):
        # Filter to 12-month window
        filtered = {k: v for k, v in monthly.items() if k >= cutoff}
        if not filtered:
            continue

        repo_commits = sum(d.get("commits", 0) for d in filtered.values())
        if repo_commits == 0:
            continue

        repo_committers = set()
        for d in filtered.values():
            repo_committers.update(d.get("committer_list", []))
            all_committers.update(d.get("committer_list", []))

        total_commits += repo_commits
        total_prs_opened += sum(d.get("prs_opened", 0) for d in filtered.values())
        total_prs_merged += sum(d.get("prs_merged", 0) for d in filtered.values())

        # Build output monthly (strip committer_list)
        output_monthly = {}
        for month, data in sorted(filtered.items()):
            entry = {"commits": data.get("commits", 0), "committers": data.get("committers", 0)}
            if "prs_opened" in data:
                entry["prs_opened"] = data["prs_opened"]
                entry["prs_merged"] = data["prs_merged"]
                entry["prs_closed"] = data.get("prs_closed", 0)
            output_monthly[month] = entry

        active_repos.append({
            "repo_name": repo_name,
            "commits": repo_commits,
            "committers": len(repo_committers),
            "monthly": output_monthly,
        })

    # Sort by commits descending (most active repos first)
    active_repos.sort(key=lambda r: r["commits"], reverse=True)

    result = {
        "project": project,
        "vcs": vcs,
        "active_repos": active_repos,
        "totals": {
            "commits": total_commits,
            "committers": len(all_committers),
        },
    }

    if vcs == "github":
        result["org"] = org or cache.get("_org", "apache")
        result["repos"] = repos or cache.get("_repos", [])
        result["totals"]["prs_opened"] = total_prs_opened
        result["totals"]["prs_merged"] = total_prs_merged
    elif vcs == "svn":
        result["svn_url"] = svn_url or cache.get("_svn_url", "")

    return result



def _empty_result(project: str, vcs: str, url: str) -> dict:
    """Return an empty result dict when collection fails."""
    return {
        "project": project,
        "vcs": vcs,
        "url": url,
        "active_repos": [],
        "totals": {"commits": 0, "committers": 0},
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def collect_git_activity(project: str, config: dict) -> dict | None:
    """Collect VCS activity metrics for a project.

    Determines the correct backend (github/svn) from config and
    dispatches accordingly. Uses caching: past months are immutable,
    only the current month is re-fetched.

    Args:
        project: ASF project name.
        config: Full asfmetrics config dict.

    Returns:
        Activity dict with monthly time-series, or None on failure.
    """
    git_config = config.get("data_sources", {}).get("git", {})
    overrides = config.get("project_overrides", {}).get(project, {})

    # Determine VCS backend for this project
    vcs = overrides.get("vcs", git_config.get("backend", "github"))

    if vcs == "svn":
        # SVN backend
        svn_url = overrides.get("svn_path")
        if not svn_url:
            svn_url = SVN_PATH_CONVENTIONS.get(project)
        if not svn_url:
            svn_url = f"{ASF_SVN_BASE}/{project}/trunk"

        return collect_svn_activity(project, svn_url, config)

    elif vcs == "github":
        # GitHub backend
        org = overrides.get("github_org", git_config.get("org", "apache"))
        token = resolve_github_token(config)

        # Determine repos for this project
        repos = overrides.get("repos")
        if repos is None:
            # Try to load from the project map (built by github_repos.py)
            json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
            project_map_path = json_dir / "_project_map.json"
            if project_map_path.exists():
                with open(project_map_path) as f:
                    project_map = json.load(f)
                if project in project_map:
                    repos = [
                        r["name"]
                        for r in project_map[project].get("repos", [])
                        if not r.get("archived")
                    ]

            # Fallback: just the project name
            if not repos:
                repos = [project]

        return collect_github_activity(project, repos, org, token, config)

    else:
        print(f"    warning: unknown VCS backend '{vcs}' for {project}")
        return None
