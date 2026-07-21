# ComDev Metrics Site — Execution Plan

Public, automated dashboard at **community.apache.org/metrics/** showing
activity of every Apache project with 12-month rolling trends.

## Design Principles

- **Trends, not absolutes** — 12-month rolling window with linear regression trend lines
- **Aggressive caching** — past months are immutable; only the current month is refreshed
- **Python/uv** — anyone can clone and test locally with `uv run`
- **Very configurable** — a project can run it for just themselves
- **VCS-agnostic** — GitHub API by default, SVN for projects that use it
- **Public** — unlike Reporter, visible to everyone
- **Static output** — generated HTML + JS frontend, no live server
- **JSON data store** — collectors write JSON; frontend reads client-side
- **No data duplication** — link to projects.apache.org for project metadata

## Data Domains

### 1. Mailing List Metrics
- Message volume (per-list, 12-month rolling window)
- Unique posters
- New-vs-returning poster ratio
- Response time (median time to first reply)
- Source: Pony Mail Foal API (`POST /api/stats.json`)
- **Caching**: Past months immutable; only current month re-fetched

### 2. Git/VCS Metrics
- Commits per month (12-month window)
- Unique committers per month
- PRs opened / merged / closed (GitHub only)
- Source: GitHub API (zero-checkout, API-only) OR `svn log --xml` (remote, no checkout)
- **Auto-detection**: SVN-only projects detected from `repositories.json`
- **Per-project VCS config**: `project_overrides` in config.yml

### 3. Community Events
- PMC member additions
- New committers
- Releases published
- Source: projects.apache.org JSON, Whimsy roster, reporter.apache.org

### 4. Project Lifecycle
- New projects (graduated from Incubator)
- Retirements to Attic
- Source: Incubator status page, board minutes

## Milestones

| # | Milestone | Target | Status |
|---|-----------|--------|--------|
| 1 | Scaffold project (pyproject.toml, config loading, CLI) | | ✅ Done |
| 2 | Mailing list collector (Pony Mail Foal API, 12-month window + caching) | | ✅ Done |
| 3 | Git/GitHub collector + SVN backend (per-repo, rate-limit aware) | | ✅ Done |
| 4 | Trend analysis (12-month rolling window + trend lines with current-month extrapolation) | | ✅ Done |
| 5 | Static HTML dashboard + per-project pages (default: All Projects tab) | | ✅ Done |
| 6 | Roster change detection (projects.apache.org JSON diffing) | | ✅ Done |
| 7 | Deploy to ComDev VM for demo | | ⬜ |
| 8 | Single-project self-serve mode | | ⬜ |
| 9 | Next Committer integration (PMC-only, LDAP gated) | | ⬜ |
| 10 | Production deployment on ComDev VM | | ⬜ |

**Status:** Dashboard running at https://boxofclue.com/comdev-metrics/

with 205 projects collecting successfully. Code at https://github.com/apache/comdev-metrics (pending).

## Architecture Notes

### Caching Strategy
- **Mailing lists**: `site/data/_cache/mailing_lists/<project>.json`
- **Git activity**: `site/data/_cache/git/<project>.json` (per-repo monthly data)
- Past months are immutable — data cannot change after a month ends
- Same-day re-runs skip (cache hit); next-day runs do incremental refresh from last fetch date
- Incremental refresh overlaps by re-fetching from the fetch date (not day-after) to avoid gaps
- `--force-refresh` clears all caches
- `--refresh-repos` re-fetches the GitHub org repo inventory

### Trend Lines
- Linear regression (least-squares) over the 12-month data series
- Current (incomplete) month is extrapolated to full-month projection before regression
- Extrapolation: `projected = actual × (days_in_month / day_of_month)`
- Rendered as dashed SVG overlay on per-project bar charts

### Per-project Detail Pages
- Link to projects.apache.org for full metadata (roster, repos, homepage)
- No roster duplication — just show PMC size stat and link out
- Git section: clickable table of repos (sparklines), click to expand chart
- Mailing list section: same pattern — clickable list table, chart on click
- First/most-active item auto-expanded on page load
- Releases collapsed after 10 with "show more" expander

### Rate Limiting (GitHub API)
- Tracks `x-ratelimit-remaining` from every response
- Auto-pauses and waits for reset when remaining ≤ 50
- Budget displayed inline every 20 projects + at end of git phase

## Deployment

### Development/Staging — boxofclue.com/comdev-metrics/

```
# Weekly cron
0 6 * * 1  rcbowen  cd /home/rbowen/devel/apache/comdev/comdev-metrics && /usr/bin/uv run asfmetrics --config /home/rbowen/devel/apache/comdev/comdev-metrics/config.yml && rsync -az --delete site/ fagin.rcbowen.com:/var/www/vhosts/boxofclue.com/comdev-metrics/
```

Output served by httpd vhost pointing at `site/`.

### Production — ComDev VM (Ubuntu)

Same cron pattern, output at `community.apache.org/metrics/` (subdirectory
or separate vhost — TBD).

## Open Questions

| Question | Notes |
|----------|-------|
| Definitive data sources? | projects.a.o is secondary and may go away. Whimsy? LDAP? |
| Next Committer ↔ LDAP? | PMC-only access. Needs ASF Infra ticket for service account or OAuth. |
| Bot filtering? | Measure contributions by PMC, committers, everyone else — and bots separately |
| Static site hosting? | Subdirectory of community.apache.org? Separate vhost? |

## Configuration

Lookup order:
1. `./config.yml` (project-local)
2. `~/.asfmetrics/config.yml` (user-level)
3. `/etc/asfmetrics/config.yml` (system-level, for ComDev VM)

See `config.example.yml`, `.secrets.example`, and `DATA_SOURCES.md` for details.
