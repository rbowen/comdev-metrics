"""Mailing list discovery and metrics collector using the Pony Mail Foal API.

On each run:
1. Discovers all mailing lists via preferences.json
2. Filters to lists with traffic in the last 12 months ("recently active")
3. Collects volume/poster stats for active lists

Aggressive caching:
  - Past months are immutable. Once we have data for a completed month,
    we never re-fetch it.
  - Only the current month (which is still in progress) is refreshed on
    each run.
  - Cache stored in: <json_dir>/_cache/mailing_lists/<project>.json

API Reference: Pony Mail Foal uses POST with JSON body to /api/*.json.
See: incubator-ponymail-foal/docs/API.md
"""

import json
from datetime import datetime
from pathlib import Path

import httpx


PONYMAIL_API = "https://lists.apache.org/api/"

# Lists with no messages in this window are excluded from the dashboard
ACTIVITY_THRESHOLD = "12M"  # Full year — trends need context beyond a single quarter


def _current_month_str() -> str:
    """Return current month as YYYY-MM."""
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"


def _cache_dir(config: dict) -> Path:
    """Return the mailing list cache directory."""
    json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
    cache_dir = json_dir / "_cache" / "mailing_lists"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def invalidate_cache(config: dict) -> None:
    """Remove all mailing list cache files.

    Used by --force-refresh to force a full re-fetch.
    """
    cache_dir = _cache_dir(config)
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            f.unlink()
        print(f"    cleared {cache_dir}")


def _load_cache(project: str, config: dict) -> dict | None:
    """Load cached mailing list data for a project.

    Returns:
        Cache dict with per-list monthly data, or None if no cache.
    """
    cache_path = _cache_dir(config) / f"{project}.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(project: str, cache_data: dict, config: dict) -> None:
    """Save mailing list cache for a project."""
    cache_path = _cache_dir(config) / f"{project}.json"
    with open(cache_path, "w") as f:
        json.dump(cache_data, f, indent=2)


def _cache_is_current(cache: dict) -> bool:
    """Check if cache has already been fetched this month.

    If _fetched_at matches the current month, all data is current
    (past months are immutable, and the current month was fetched today
    or at least this month).
    """
    fetched_at = cache.get("_fetched_at", "")
    # Compare YYYY-MM portion
    return fetched_at[:7] == _current_month_str()


def discover_lists(base_url: str = PONYMAIL_API) -> dict:
    """Discover all mailing lists from the Pony Mail preferences endpoint.

    Returns:
        Dict of {domain: {list_name: count, ...}, ...}
        e.g. {"httpd.apache.org": {"dev": 1523, "users": 890}}
    """
    url = f"{base_url}preferences.json"
    try:
        resp = httpx.post(url, json={}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"    warning: failed to discover lists: {e}")
        return {}

    return data.get("lists", {})


def collect_list_stats(
    list_name: str,
    domain: str,
    base_url: str = PONYMAIL_API,
    timespan: str = ACTIVITY_THRESHOLD,
    quick: bool = True,
) -> dict | None:
    """Fetch stats for a single mailing list.

    Uses the Foal stats.json endpoint (POST with JSON body).

    Args:
        list_name: List name prefix (e.g. "dev").
        domain: List domain (e.g. "httpd.apache.org").
        base_url: Pony Mail API base URL.
        timespan: Date filter in Pony Mail format (e.g. "6M", "3M").
        quick: If True, return stats only (no email bodies).

    Returns:
        Stats dict with hits, numparts, no_threads, etc. or None on failure.
    """
    url = f"{base_url}stats.json"
    payload = {
        "list": list_name,
        "domain": domain,
        "d": f"lte={timespan}",
    }
    if quick:
        payload["quick"] = True

    try:
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None

    # No messages in this window
    if data.get("hits", 0) == 0:
        return None

    return data


def _fetch_current_month_stats(
    list_name: str,
    domain: str,
    base_url: str = PONYMAIL_API,
) -> dict | None:
    """Fetch stats for just the current month (1M window).

    Used for incremental cache updates — only refreshes the current month.
    """
    return collect_list_stats(list_name, domain, base_url, timespan="1M", quick=True)


def collect_mailing_list_stats(project: str, config: dict) -> dict | None:
    """Collect mailing list statistics for a single project, with caching.

    Caching behavior:
      - If cache exists and was fetched this month: return cached data (no API calls).
      - If cache exists but from a previous month: only refresh current month data.
      - If no cache: full fetch, then cache the result.

    Past months are immutable — once a month ends, its data never changes.
    Only the current (in-progress) month needs refreshing.

    Args:
        project: ASF project name (e.g. 'kafka', 'comdev').
        config: Full config dict.

    Returns:
        Dict with per-list stats, or None if no active lists found.
    """
    base_url = (
        config.get("data_sources", {})
        .get("mailing_lists", {})
        .get("base_url", PONYMAIL_API)
    )
    timespan = (
        config.get("data_sources", {})
        .get("mailing_lists", {})
        .get("activity_threshold", ACTIVITY_THRESHOLD)
    )

    # ComDev's domain is community.apache.org, not comdev.apache.org
    domain_overrides = {
        "comdev": "community.apache.org",
        "infrastructure": "infra.apache.org",
    }
    domain = domain_overrides.get(project, f"{project}.apache.org")

    # Check cache
    cache = _load_cache(project, config)
    current_month = _current_month_str()

    if cache and _cache_is_current(cache):
        # Cache is fresh — return directly without any API calls
        return _build_result_from_cache(project, domain, timespan, cache)

    if cache and cache.get("lists"):
        # Cache exists but stale (from a previous month).
        # Past months are immutable — only refresh the current month.
        updated = False
        for list_id, list_data in cache.get("lists", {}).items():
            list_name = list_data.get("list_name", list_id.split("@")[0])
            stats = _fetch_current_month_stats(list_name, domain, base_url)
            if stats:
                months = stats.get("active_months", {})
                # Update just the current month in the cached months
                cached_months = list_data.get("active_months", {})
                if current_month in months:
                    cached_months[current_month] = months[current_month]
                elif current_month not in cached_months:
                    # API returned no data for current month — set to 0
                    # (or leave missing, either is fine)
                    pass
                list_data["active_months"] = cached_months
                # Update totals from the full month range
                list_data["messages"] = sum(
                    v for k, v in cached_months.items()
                    if k >= _twelve_months_ago_str()
                )
                list_data["participants"] = stats.get("numparts", list_data.get("participants", 0))
                list_data["threads"] = stats.get("no_threads", list_data.get("threads", 0))
                updated = True

        if updated:
            cache["_fetched_at"] = datetime.now().strftime("%Y-%m-%d")
            _save_cache(project, cache, config)

        return _build_result_from_cache(project, domain, timespan, cache)

    # No cache — full fetch
    # Discover all lists for this domain from preferences
    all_domain_lists = discover_lists(base_url)
    project_lists = list(all_domain_lists.get(domain, {}).keys())

    # If we can't discover, fall back to conventional names
    if not project_lists:
        project_lists = ["dev", "user", "users", "general"]

    active_lists = []
    cache_lists = {}

    for list_name in project_lists:
        stats = collect_list_stats(list_name, domain, base_url, timespan)
        if stats:
            list_id = f"{list_name}@{domain}"
            active_months = stats.get("active_months", {})

            list_entry = {
                "list_name": list_name,
                "list_id": list_id,
                "messages": stats.get("hits", 0),
                "participants": stats.get("numparts", 0),
                "threads": stats.get("no_threads", 0),
                "active_months": active_months,
            }
            active_lists.append(list_entry)
            cache_lists[list_id] = list_entry

    if not active_lists:
        return None

    # Save cache
    cache_data = {
        "_fetched_at": datetime.now().strftime("%Y-%m-%d"),
        "_project": project,
        "_domain": domain,
        "lists": cache_lists,
    }
    _save_cache(project, cache_data, config)

    return {
        "project": project,
        "domain": domain,
        "timespan": timespan,
        "active_lists": active_lists,
    }


def _twelve_months_ago_str() -> str:
    """Return YYYY-MM string for 12 months ago."""
    now = datetime.now()
    year = now.year - 1
    month = now.month
    return f"{year}-{month:02d}"


def _build_result_from_cache(
    project: str,
    domain: str,
    timespan: str,
    cache: dict,
) -> dict | None:
    """Reconstruct the API result format from cached data.

    Filters to the 12-month window for the output.
    """
    cutoff = _twelve_months_ago_str()
    active_lists = []

    for list_id, list_data in cache.get("lists", {}).items():
        all_months = list_data.get("active_months", {})
        # Filter to 12-month window
        recent_months = {
            k: v for k, v in all_months.items() if k >= cutoff
        }
        if not recent_months:
            continue

        active_lists.append({
            "list_name": list_data.get("list_name", list_id.split("@")[0]),
            "list_id": list_id,
            "messages": sum(recent_months.values()),
            "participants": list_data.get("participants", 0),
            "threads": list_data.get("threads", 0),
            "active_months": recent_months,
        })

    if not active_lists:
        return None

    return {
        "project": project,
        "domain": domain,
        "timespan": timespan,
        "active_lists": active_lists,
    }


def discover_all_active_projects(config: dict) -> list[str]:
    """Discover all projects that have at least one recently-active list.

    Queries the preferences.json endpoint to get all known lists,
    then returns projects whose domain has any list with recorded messages.

    Args:
        config: Full config dict.

    Returns:
        Sorted list of project names with mailing list activity.
    """
    base_url = (
        config.get("data_sources", {})
        .get("mailing_lists", {})
        .get("base_url", PONYMAIL_API)
    )

    print("    discovering mailing lists from Pony Mail...")
    all_lists = discover_lists(base_url)
    print(f"    found {len(all_lists)} domains")

    # Domain -> project name mapping
    # Most are simply project.apache.org, but some are different
    reverse_domain = {
        "community.apache.org": "comdev",
        "infra.apache.org": "infrastructure",
    }

    active_projects = set()
    for domain, lists in all_lists.items():
        if not domain.endswith(".apache.org"):
            continue
        # Check if any list in this domain has messages
        if any(count > 0 for count in lists.values()):
            project = reverse_domain.get(domain, domain.replace(".apache.org", ""))
            active_projects.add(project)

    print(f"    {len(active_projects)} projects with mailing list activity")
    return sorted(active_projects)
