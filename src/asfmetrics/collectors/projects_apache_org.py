"""Collector that fetches pre-built JSON from projects.apache.org.

This is the pragmatic "get started quick" data source. These JSON files
are generated daily by comdev-projects cronjobs from authoritative sources
(committee-info.txt, LDAP, DOAP files, etc.) and published publicly.

Later we can swap individual data points for direct Whimsy/LDAP/GitHub
queries, but this gets us a full dataset on day one with zero auth.

Public JSON base: https://projects.apache.org/json/foundation/
"""

import json
from datetime import datetime
from pathlib import Path

import httpx


BASE_URL = "https://projects.apache.org/json/foundation/"

# Files we fetch and cache locally
FOUNDATION_FILES = {
    "committees.json": "PMC roster, chairs, charters, established dates",
    "committees-retired.json": "Retired committees",
    "people.json": "Committer → project membership mapping",
    "podlings.json": "Current podlings + status",
    "podlings-history.json": "Podling lifecycle (start, graduation, retirement dates)",
    "releases.json": "Release history per project",
    "repositories.json": "Git/SVN repository listing",
    "accounts-evolution2.json": "Account creation over time",
    "projects.json": "Master project metadata (from DOAP files)",
}

# Whimsy public JSON for committer creation dates
WHIMSY_LDAP_PEOPLE = "https://whimsy.apache.org/public/public_ldap_people.json"
# Also fetch committee-info.json from Whimsy for PMC dates (more authoritative)
WHIMSY_COMMITTEE_INFO = "https://whimsy.apache.org/public/committee-info.json"


def fetch_foundation_json(
    filename: str, base_url: str = BASE_URL, timeout: int = 30
) -> dict | list | None:
    """Fetch a single foundation JSON file from projects.apache.org.

    Args:
        filename: JSON filename (e.g. 'committees.json').
        base_url: Base URL for the JSON files.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON (dict or list), or None on failure.
    """
    url = f"{base_url}{filename}"
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"    warning: failed to fetch {filename}: {e}")
        return None


def fetch_all_foundation_data(config: dict) -> dict:
    """Fetch all foundation JSON files and cache locally.

    Args:
        config: Full asfmetrics config dict.

    Returns:
        Dict keyed by filename (without .json), value is parsed data.
    """
    base_url = (
        config.get("data_sources", {})
        .get("projects_apache_org", {})
        .get("base_url", BASE_URL)
    )
    cache_dir = Path(
        config.get("output", {}).get("json_dir", "./site/data/")
    ) / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    data = {}
    for filename, description in FOUNDATION_FILES.items():
        key = filename.replace(".json", "").replace("-", "_")
        print(f"    fetching {filename} ({description})...")
        content = fetch_foundation_json(filename, base_url)
        if content is not None:
            data[key] = content
            # Cache locally for offline/debugging use
            cache_path = cache_dir / filename
            with open(cache_path, "w") as f:
                json.dump(content, f, indent=2)

    print(f"    fetched {len(data)}/{len(FOUNDATION_FILES)} files successfully")
    return data


def extract_active_projects(data: dict) -> list[str]:
    """Get sorted list of active project names — TLPs + current podlings.

    Args:
        data: Dict from fetch_all_foundation_data().

    Returns:
        Sorted list of project IDs (e.g. ['accumulo', 'airflow', ..., 'ponymail', ...]).
    """
    projects = set()

    # TLPs from committees.json
    committees = data.get("committees", [])
    if isinstance(committees, list):
        for c in committees:
            if "id" in c:
                projects.add(c["id"])

    # Current podlings from podlings.json
    podlings = data.get("podlings", {})
    if isinstance(podlings, dict):
        for name, info in podlings.items():
            # Only include active podlings (status = "current")
            status = info.get("status", "").lower() if isinstance(info, dict) else ""
            if status == "current":
                projects.add(name.lower())
    elif isinstance(podlings, list):
        for p in podlings:
            if isinstance(p, dict) and p.get("status", "").lower() == "current":
                projects.add(p.get("name", p.get("id", "")).lower())

    return sorted(projects)


def detect_roster_changes(current_data: dict, state_dir: Path) -> dict:
    """Detect new committers/PMC members by comparing roster dates.

    Compares current committees.json roster entries against the previous
    cached version. Any roster entry with a 'date' field newer than the
    last run is flagged as a new addition.

    Args:
        current_data: Dict from fetch_all_foundation_data().
        state_dir: Path to directory containing previous state.

    Returns:
        Dict with: new_committers (list), new_pmcs (list of graduated),
        retired (list).
    """
    changes = {
        "new_roster_entries": [],  # {project, person, date}
        "newly_retired": [],
    }

    state_path = state_dir / "_committees_previous.json"
    committees = current_data.get("committees", [])

    if not isinstance(committees, list):
        return changes

    # Build current roster snapshot
    current_roster = {}
    for pmc in committees:
        project_id = pmc.get("id", "")
        roster = pmc.get("roster", {})
        for uid, info in roster.items():
            current_roster[f"{project_id}/{uid}"] = info.get("date", "")

    # Compare against previous
    if state_path.exists():
        with open(state_path) as f:
            previous_roster = json.load(f)

        new_entries = set(current_roster.keys()) - set(previous_roster.keys())
        for key in sorted(new_entries):
            project_id, uid = key.split("/", 1)
            changes["new_roster_entries"].append({
                "project": project_id,
                "person": uid,
                "date": current_roster[key],
            })

    # Detect newly retired (compare retired lists)
    retired = current_data.get("committees_retired", [])
    retired_state_path = state_dir / "_retired_previous.json"
    if isinstance(retired, list) and retired_state_path.exists():
        with open(retired_state_path) as f:
            prev_retired = json.load(f)
        prev_ids = {r.get("id") for r in prev_retired if isinstance(r, dict)}
        curr_ids = {r.get("id") for r in retired if isinstance(r, dict)}
        changes["newly_retired"] = sorted(curr_ids - prev_ids)

    # Save current state for next run
    with open(state_path, "w") as f:
        json.dump(current_roster, f)
    if isinstance(retired, list):
        with open(retired_state_path, "w") as f:
            json.dump(retired, f)

    return changes


def collect_new_committers(data: dict, config: dict) -> dict:
    """Detect new committers using Whimsy LDAP createTimestamp.

    Fetches public_ldap_people.json from Whimsy and cross-references
    with people.json to find committers created in the last 12 months,
    grouped by project.

    Returns:
        Dict with: by_project (project -> list of {name, id, date}),
                   total (int), by_month (YYYY-MM -> count)
    """
    print("    fetching Whimsy LDAP data for new committer dates...")

    try:
        resp = httpx.get(WHIMSY_LDAP_PEOPLE, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        ldap_data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"    warning: failed to fetch Whimsy LDAP data: {e}")
        return {"by_project": {}, "total": 0, "by_month": {}}

    people_data = data.get("people", {})
    ldap_people = ldap_data.get("people", ldap_data)

    # 12-month cutoff
    now = datetime.now()
    cutoff = now.replace(year=now.year - 1)

    new_committers_by_project = {}
    by_month = {}
    total = 0

    for person_id, person_info in people_data.items():
        if person_id not in ldap_people:
            continue

        ldap_entry = ldap_people[person_id]
        create_ts = ldap_entry.get("createTimestamp", "")
        if not create_ts:
            continue

        try:
            created = datetime.strptime(create_ts, "%Y%m%d%H%M%SZ")
        except (ValueError, TypeError):
            continue

        if created < cutoff:
            continue

        # This person was created in the last 12 months — they're a new committer
        month_key = created.strftime("%Y-%m")
        by_month[month_key] = by_month.get(month_key, 0) + 1
        total += 1

        for group in person_info.get("groups", []):
            # Skip meta-groups
            if group.endswith("-pmc") or group in ("apldap", "incubator", "member"):
                continue
            if group not in new_committers_by_project:
                new_committers_by_project[group] = []
            new_committers_by_project[group].append({
                "name": person_info.get("name", person_id),
                "id": person_id,
                "date": created.strftime("%Y-%m-%d"),
            })

    print(f"    new committers (12mo): {total} across {len(new_committers_by_project)} projects")

    return {
        "by_project": new_committers_by_project,
        "total": total,
        "by_month": by_month,
    }


def collect_projects_apache_org(config: dict) -> dict:
    """Main entry point: fetch all data, detect changes, return summary.

    Args:
        config: Full asfmetrics config dict.

    Returns:
        Dict with foundation data, active projects, and detected changes.
    """
    print("  collecting from projects.apache.org...")
    data = fetch_all_foundation_data(config)

    json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
    state_dir = json_dir / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    active_projects = extract_active_projects(data)
    roster_changes = detect_roster_changes(data, state_dir)

    # Detect new committers from Whimsy LDAP
    new_committers = collect_new_committers(data, config)

    # Save new committers data for the frontend
    cache_dir = json_dir / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_dir / "new_committers.json", "w") as f:
        json.dump(new_committers, f, indent=2)

    print(f"    {len(active_projects)} active projects")
    if roster_changes["new_roster_entries"]:
        print(f"    {len(roster_changes['new_roster_entries'])} new roster entries since last run")
    if roster_changes["newly_retired"]:
        print(f"    {len(roster_changes['newly_retired'])} newly retired projects")

    return {
        "active_projects": active_projects,
        "roster_changes": roster_changes,
        "new_committers": new_committers,
        "data": data,
    }
