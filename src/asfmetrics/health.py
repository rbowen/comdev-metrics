"""Deterministic project health classification.

Reads per-project mailing list and git JSON from site/data/,
computes quarter-over-quarter trends, and classifies projects as
Declining, At Risk, or Dormant.

All thresholds and logic are fixed — no ML/LLM involved.

Output: site/data/_cache/project_health.json
"""

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


# --- Thresholds (deterministic) ---
DECLINE_THRESHOLD = -30  # percent drop to count as "declining"
MODERATE_DECLINE = -15   # percent drop for "at risk" when both axes
MIN_PRIOR_ACTIVITY = 30  # min msgs or commits in prior quarter to be "significant"
DORMANT_COMMITS = 5      # max commits in recent quarter to be dormant
DORMANT_MESSAGES = 5     # max messages in recent quarter to be dormant

# Mailing lists that represent human discussion (not automated)
DISCUSSION_LISTS = {"dev", "user", "users", "general", "discuss"}


def _determine_quarters(today: date = None) -> dict:
    """Determine the two most recent full quarters for comparison.

    We always exclude the current (partial) month. The "recent" quarter
    is the 3 full months ending before the current month. The "prior"
    quarter is the 3 months before that.

    Returns dict with month lists and display labels.
    """
    if today is None:
        today = date.today()

    # Current month is partial — exclude it.
    # Recent quarter = the 3 most recent full months.
    # Prior quarter = the 3 months before that.
    # e.g., if today is Jul 10 2026: recent = Apr, May, Jun; prior = Jan, Feb, Mar
    current_month = today.replace(day=1)
    months = []
    m = current_month
    for _ in range(6):  # 6 full months back from current partial month
        m = (m - timedelta(days=1)).replace(day=1)
        months.append(m.strftime("%Y-%m"))
    months.reverse()  # oldest first

    prior_months = months[0:3]
    recent_months = months[3:6]

    # Labels
    def quarter_label(month_list):
        first = date.fromisoformat(f"{month_list[0]}-01")
        last = date.fromisoformat(f"{month_list[-1]}-01")
        return f"{first.strftime('%b')}–{last.strftime('%b %Y')}"

    return {
        "prior_months": prior_months,
        "recent_months": recent_months,
        "prior_label": quarter_label(prior_months),
        "recent_label": quarter_label(recent_months),
        "window_description": (
            f"Comparing {quarter_label(recent_months)} (recent) "
            f"vs {quarter_label(prior_months)} (prior). "
            f"Current partial month ({today.strftime('%b %Y')}) excluded."
        ),
    }


def _compute_trend(monthly_data: dict, recent_months: list, prior_months: list):
    """Compute trend between two sets of months.

    Returns (percent_change, recent_total, prior_total).
    percent_change is None if prior_total == 0.
    """
    recent_total = sum(monthly_data.get(m, 0) for m in recent_months)
    prior_total = sum(monthly_data.get(m, 0) for m in prior_months)

    if prior_total == 0:
        pct = None
    else:
        pct = round(((recent_total - prior_total) / prior_total) * 100, 1)

    return pct, recent_total, prior_total


def compute_project_health(config: dict) -> dict:
    """Compute health classifications for all projects.

    Reads existing per-project JSON files from the output directory.
    Writes _cache/project_health.json.

    Args:
        config: Full asfmetrics config dict.

    Returns:
        The health data dict (also written to disk).
    """
    json_dir = Path(config.get("output", {}).get("json_dir", "./site/data/"))
    cache_dir = json_dir / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    quarters = _determine_quarters()
    recent_months = quarters["recent_months"]
    prior_months = quarters["prior_months"]

    # Discover all projects (those with a .json file that isn't _* or *_git)
    projects = set()
    for f in json_dir.glob("*.json"):
        name = f.stem
        if name.startswith("_") or name.endswith("_git"):
            continue
        projects.add(name)

    declining = []
    at_risk = []
    dormant = []

    for proj in sorted(projects):
        ml_path = json_dir / f"{proj}.json"
        git_path = json_dir / f"{proj}_git.json"

        # --- Mailing list analysis (discussion lists only) ---
        ml_months = defaultdict(int)
        if ml_path.exists():
            try:
                with open(ml_path) as f:
                    ml_data = json.load(f)
                for lst in ml_data.get("active_lists", []):
                    if lst.get("list_name", "") in DISCUSSION_LISTS:
                        for month, count in lst.get("active_months", {}).items():
                            ml_months[month] += count
            except (json.JSONDecodeError, OSError):
                pass

        ml_pct, ml_recent, ml_prior = _compute_trend(
            dict(ml_months), recent_months, prior_months
        )

        # --- Git analysis (total commits across all repos) ---
        git_months = defaultdict(int)
        git_total_12mo = 0
        if git_path.exists():
            try:
                with open(git_path) as f:
                    git_data = json.load(f)
                for repo in git_data.get("active_repos", []):
                    git_total_12mo += repo.get("commits", 0)
                    for month, stats in repo.get("monthly", {}).items():
                        git_months[month] += stats.get("commits", 0)
            except (json.JSONDecodeError, OSError):
                pass

        git_pct, git_recent, git_prior = _compute_trend(
            dict(git_months), recent_months, prior_months
        )

        # --- Classification ---
        entry = {
            "project": proj,
            "ml_trend_pct": ml_pct,
            "ml_recent": ml_recent,
            "ml_prior": ml_prior,
            "git_trend_pct": git_pct,
            "git_recent": git_recent,
            "git_prior": git_prior,
            "git_total_12mo": git_total_12mo,
        }

        # Dormant: near-zero activity everywhere
        if ml_recent <= DORMANT_MESSAGES and git_recent <= DORMANT_COMMITS and git_total_12mo <= 20:
            dormant.append(entry)
            continue

        ml_declining = ml_pct is not None and ml_pct <= DECLINE_THRESHOLD
        git_declining = git_pct is not None and git_pct <= DECLINE_THRESHOLD
        ml_moderate = ml_pct is not None and ml_pct <= MODERATE_DECLINE
        git_moderate = git_pct is not None and git_pct <= MODERATE_DECLINE
        ml_growing = ml_pct is not None and ml_pct >= -DECLINE_THRESHOLD  # +30% or more
        git_growing = git_pct is not None and git_pct >= -DECLINE_THRESHOLD

        # Declining: both axes down sharply
        if ml_declining and git_declining:
            declining.append(entry)
        elif ml_pct is not None and ml_pct <= -60 and git_moderate:
            declining.append(entry)
        elif git_pct is not None and git_pct <= -60 and ml_moderate:
            declining.append(entry)
        # At Risk: one axis down sharply (with meaningful prior activity)
        # But NOT if the other axis is growing strongly (compensating)
        elif ml_declining and not git_growing and (ml_prior >= MIN_PRIOR_ACTIVITY or git_prior >= MIN_PRIOR_ACTIVITY):
            at_risk.append(entry)
        elif git_declining and not ml_growing and (ml_prior >= MIN_PRIOR_ACTIVITY or git_prior >= MIN_PRIOR_ACTIVITY):
            at_risk.append(entry)
        elif ml_moderate and git_moderate and not ml_growing and not git_growing and (ml_prior >= MIN_PRIOR_ACTIVITY or git_prior >= MIN_PRIOR_ACTIVITY):
            at_risk.append(entry)

    # Sort by severity (combined trend percentage, most negative first)
    def severity(e):
        ml = e["ml_trend_pct"] if e["ml_trend_pct"] is not None else 0
        git = e["git_trend_pct"] if e["git_trend_pct"] is not None else 0
        return ml + git

    declining.sort(key=severity)
    at_risk.sort(key=severity)
    dormant.sort(key=lambda e: e["git_total_12mo"] + e["ml_recent"])

    result = {
        "generated": date.today().isoformat(),
        "window": {
            "recent_months": recent_months,
            "prior_months": prior_months,
            "recent_label": quarters["recent_label"],
            "prior_label": quarters["prior_label"],
            "description": quarters["window_description"],
        },
        "thresholds": {
            "decline_pct": DECLINE_THRESHOLD,
            "moderate_decline_pct": MODERATE_DECLINE,
            "min_prior_activity": MIN_PRIOR_ACTIVITY,
            "dormant_commits": DORMANT_COMMITS,
            "dormant_messages": DORMANT_MESSAGES,
        },
        "summary": {
            "total_projects": len(projects),
            "declining_count": len(declining),
            "at_risk_count": len(at_risk),
            "dormant_count": len(dormant),
        },
        "declining": declining,
        "at_risk": at_risk,
        "dormant": dormant,
    }

    # Write output
    output_path = cache_dir / "project_health.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"  project health: {len(declining)} declining, "
          f"{len(at_risk)} at risk, {len(dormant)} dormant "
          f"(of {len(projects)} total)")

    return result
