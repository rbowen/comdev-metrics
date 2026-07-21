# ASF Community Development Metrics

Public dashboard showing community activity metrics for Apache projects,
with 12-month trend lines for meaningful context.

**Status**: Working dashboard deployed at https://boxofclue.com/comdev-metrics/ —
fetches foundation data from projects.apache.org, mailing list stats from
Pony Mail Foal API, Git/GitHub activity (with SVN support for legacy projects),
and renders a per-project dashboard with trend lines, releases, and list activity.

## Quick Start

```bash
cd /path/to/comdev-metrics
cp config.example.yml config.yml
cp .secrets.example .secrets
# Edit .secrets with your GitHub token
# Edit config.yml as needed

# Run with uv (no install needed)
uv run asfmetrics

# Single project
asfmetrics --project comdev

# Skip mailing lists (just fetch foundation + git data)
asfmetrics --skip-mailing-lists

# Skip git/VCS collection
asfmetrics --skip-git

# Force re-fetch everything (bypass cache)
asfmetrics --force-refresh

# View results
cd site && python3 -m http.server 8888
# Open http://localhost:8888
```

## What It Does

1. **Fetches** foundation-wide data from projects.apache.org JSON files (committees, rosters, releases, podlings)
2. **Discovers** all mailing lists per project via Pony Mail Foal `preferences.json`
3. **Collects** per-list message stats for a 12-month rolling window (with aggressive caching — past months are never re-fetched)
4. **Collects** Git/VCS activity per project (commits, committers, PRs opened/merged) via GitHub API or SVN log
5. **Detects** roster changes (new committers/PMC members) by diffing between runs
6. **Writes** everything to a JSON data store (`site/data/`)
7. **Publishes** a static HTML + JS dashboard with:
   - Overview: community growth, releases, mailing list activity rankings
   - Per-project pages: all active lists, monthly bar charts with trend lines, releases
   - Links to projects.apache.org for full project metadata (roster, repos, homepage)

Only lists with actual traffic in the 12-month window are shown.

## Caching

Mailing list data is aggressively cached to minimize API calls:

- **Past months are immutable.** Once a month ends, its message counts can never
  change in the archive. Cached data for completed months is never re-fetched.
- **Only the current month is refreshed** on each run (since it's still in progress).
- **Cache location:** `site/data/_cache/mailing_lists/<project>.json`
- **Force refresh:** Use `--force-refresh` to clear all caches and re-fetch everything.

On a typical weekly cron run mid-month, the mailing list phase completes in seconds
(cache hits) instead of making hundreds of API calls.

## Configuration

See `config.example.yml`. Config is searched in order:
1. `./config.yml`
2. `~/.asfmetrics/config.yml`
3. `/etc/asfmetrics/config.yml`

### Secrets

GitHub token (needed for git activity collection; optional but strongly recommended):
1. `GITHUB_TOKEN` environment variable
2. `.secrets` file in the project directory (line: `GITHUB_TOKEN=ghp_...`)
3. `~/.secrets` file (user-level fallback)
4. `config.yml` `data_sources.git.token` field

Copy `.secrets.example` → `.secrets` and fill in your token.
The `.secrets` file is in `.gitignore` and will never be committed.

### Per-project VCS overrides

Most projects use GitHub. For projects still on Subversion, add an override
in `config.yml`:

```yaml
project_overrides:
  httpd:
    vcs: svn
    svn_path: https://svn.apache.org/repos/asf/httpd/httpd/trunk
```

See `config.example.yml` for full documentation of override options.

## Project Structure

```
comdev-metrics/
├── .secrets.example         # Template for secrets (GitHub token)
├── config.example.yml       # Example configuration
├── pyproject.toml           # uv/hatch project definition
├── src/asfmetrics/
│   ├── cli.py              # Entry point
│   ├── config.py           # Config loading
│   ├── collectors/
│   │   ├── mailing_lists.py         # Pony Mail Foal API + caching
│   │   ├── git_activity.py          # GitHub API + SVN log collector
│   │   ├── github_repos.py          # Repo inventory + project classification
│   │   └── projects_apache_org.py   # Foundation JSON fetcher + roster differ
│   ├── analysis/           # Trend calculations
│   └── output/             # JSON + HTML generation
├── site/
│   ├── index.html          # Overview dashboard (default tab: All Projects)
│   ├── project.html        # Per-project detail page (?id=projectname)
│   └── data/               # Generated JSON + cache (git-ignored)
│       ├── _cache/
│       │   └── mailing_lists/  # Per-project mailing list cache
│       ├── <project>.json      # Mailing list data
│       └── <project>_git.json  # Git activity data
├── DATA_SOURCES.md         # Where all the data comes from
├── PLAN.md                 # Execution plan and milestones
└── tests/
```

## CLI Reference

```
usage: asfmetrics [-h] [--config CONFIG] [--project PROJECT]
                  [--skip-mailing-lists] [--skip-git]
                  [--force-refresh] [--refresh-repos]

Options:
  --config CONFIG       Path to config.yml (default: auto-discover)
  --project PROJECT     Run for a single project only
  --skip-mailing-lists  Skip mailing list collection
  --skip-git            Skip git/VCS activity collection
  --force-refresh       Clear all caches and re-fetch everything
  --refresh-repos       Re-fetch the GitHub repo inventory (project → repos map)
```

## Deployment

**Dev/staging**: https://boxofclue.com/comdev-metrics/ (matrim.rcbowen.com, Alma Linux)
**Production**: ASF ComDev VM (Ubuntu) — eventually

```
# Weekly cron
0 6 * * 1  rcbowen  cd /opt/asfmetrics && uv run asfmetrics --config /etc/asfmetrics/config.yml
```

Output served by httpd vhost pointing at `site/`.

## License

Apache License 2.0
