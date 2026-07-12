# ASF Data Sources — Reference

Where does the authoritative data live?

## The Hierarchy

```
                    ┌─────────────────────┐
                    │   LDAP (id.apache.org)│  ← THE source of truth for people,
                    │   + committee-info.txt│     groups, PMC membership
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──────┐  ┌─────▼─────┐  ┌──────▼──────┐
    │  Whimsy/Roster │  │ Incubator │  │  Board      │
    │  (public JSON) │  │podlings.xml│  │  minutes    │
    └─────────┬──────┘  └─────┬─────┘  └──────┬──────┘
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ projects.apache.org  │  ← SECONDARY: aggregates from above
                    │ (DOAP + cronjobs)    │     + project-maintained DOAP files
                    └─────────────────────┘
```

## Primary/Authoritative Sources

| Data | Authoritative Source | Access | Notes |
|------|---------------------|--------|-------|
| **PMC membership** | `committee-info.txt` (private SVN) | Via Whimsy JSON | Updated by Secretary |
| **Committer list** | LDAP (`id.apache.org`) | Via Whimsy `public_ldap_projects.json` | |
| **Podling status** | `incubator/.../podlings.xml` | Via Whimsy `public_podling_status.json` | Maintained by VP Incubator |
| **Project metadata** | DOAP files (maintained by each PMC) | Listed in `data/projects.xml` on SVN | |
| **Retired projects** | `committee-info.yaml` + Attic | Via Whimsy `committee-retired.json` | |

## Whimsy Public JSON (our best bet for programmatic access)

Base URL: `https://whimsy.apache.org/public/`

| File | Contents | Useful for |
|------|----------|------------|
| `committee-info.json` | PMC names, members, chairs | Community events (PMC additions) |
| `committee-retired.json` | Retired committees | Project lifecycle |
| `public_ldap_projects.json` | PMC/podling owners + members | Committer additions |
| `public_podling_status.json` | Podling incubation status | Project lifecycle |
| `public_ldap_people.json` | Person names + disabled status | People data |
| `icla-info.json` | ICLA signers with committer IDs | |

These are regenerated hourly by Whimsy cron jobs **only when underlying data changes**.

## What projects.apache.org adds

- **DOAP aggregation**: Project descriptions, categories, programming languages, homepage URLs
- **Release data**: Scraped from various sources
- **Cronjob Python scripts**: Run daily, cache to JSON in SVN

Source: `https://svn.apache.org/repos/asf/comdev/projects.apache.org/trunk/`

## Our Strategy

### Phase 1 (MVP — DONE — use what's publicly available without auth)
- **Mailing lists**: Pony Mail API (`lists.apache.org/api/`) — no auth needed
  - Aggressive caching: past months immutable, only current month refreshed
- **Git metrics**: GitHub API (`api.github.com/orgs/apache`) — public, rate-limited (use token from `.secrets`)
  - Also supports SVN via `svn log --xml` for projects configured with `vcs: svn`
  - Per-project VCS override in `config.yml` → `project_overrides:`
  - Rate-limit tracking with automatic pause-and-wait at 50 remaining calls
  - Per-project VCS override in `config.yml` → `project_overrides:`
- **Project roster changes**: Whimsy public JSON (diff `committee-info.json` between runs)
- **Project list**: GitHub org repos + Pony Mail list discovery
- **Dashboard**: 12-month rolling window with linear regression trend lines
  - Current month extrapolated to full-month estimate for trend accuracy

### Phase 2 (richer data)
- **Release history**: projects.apache.org JSON cache or direct DOAP parsing
- **Podling lifecycle**: Whimsy `public_podling_status.json`
- **Retired projects**: Whimsy `committee-retired.json`
- **Board report status**: Would need access to board minutes

### Phase 3
- **LDAP direct queries**: Real-time committer/PMC additions?
- **Next Committer integration**: PMC-only access via LDAP auth

## Key Insight

**projects.apache.org is convenient but not authoritative.** It aggregates from:
1. DOAP files (maintained by PMCs, varying quality/currency)
2. Whimsy/LDAP (the real source of truth for people/committee data)
3. Its own cronjob caches (may lag)

For community health metrics, **Whimsy public JSON + Pony Mail + GitHub API** gives us
everything we need for Phase 1 without any special access.

## URLs Quick Reference

```
# Whimsy public data
https://whimsy.apache.org/public/committee-info.json
https://whimsy.apache.org/public/public_ldap_projects.json
https://whimsy.apache.org/public/public_podling_status.json
https://whimsy.apache.org/public/committee-retired.json

# Pony Mail
https://lists.apache.org/api/preferences.json (list discovery)
https://lists.apache.org/api/stats.json (per-list stats)

# GitHub
https://api.github.com/orgs/apache/repos        (repo listing)

# projects.apache.org data (secondary)
https://projects.apache.org/json/projects.json  (all projects metadata)
https://projects.apache.org/json/releases.json  (release data)
```
