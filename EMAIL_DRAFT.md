Subject: [DISCUSS] Community metrics dashboard - vision and feedback request

Hi, folks.

I've been working on a public metrics dashboard for Apache projects,
and I'd like to share where it's headed and get your feedback before I
deploy it more widely.

## What is it?

A static HTML dashboard (no login required, no live server) that shows
community health indicators for every Apache project with recent
activity. Think of it as a public, automated complement to the
quarterly board reports — focused on trends rather than raw numbers.

The code lives at:
https://github.com/apache/comdev-metrics (not yet — local for now, will
propose this once we agree on direction)

## What does it measure?

1. Mailing list activity — message volume, unique posters,
   new-vs-returning poster ratio, response times (via Pony Mail API)

2. Git/GitHub activity — commits, unique committers, PRs opened/merged,
   time-to-merge (via GitHub API, zero-checkout — no clones needed)

3. Community events — new committers, new PMC members, releases
   published (via projects.apache.org JSON + roster diffs)

4. Project lifecycle — graduations from Incubator, retirements to Attic

## Design principles

- Trends, not absolutes — 12-month rolling window with trend lines so
  you can see where things are headed, not just a snapshot
- Public — unlike Reporter, anyone can see the data
- Configurable — any PMC can run it for just their own project with
  `uv run asfmetrics --project yourproject`
- Static output — generated HTML + JS reads from a JSON data store,
  no live backend needed
- Python/uv — clone it, `uv run`, done

## What's working today

I have a prototype running on a personal server (weekly cron) that:
- Fetches foundation-wide data from projects.apache.org
- Collects per-list mailing stats from Pony Mail Foal API (12-month
  window, with aggressive caching so past months aren't re-fetched)
- Collects git/VCS activity — GitHub API for most projects, SVN log
  for projects on Subversion (httpd, apr, subversion)
- Detects roster changes (new committers/PMC members) between runs
- Renders a per-project dashboard page with bar charts, trend lines,
  and release history (links to projects.apache.org for roster/metadata)

## What I'm looking for

- What metrics would be most useful to YOU as a PMC member or
  committer? What's missing from my list above?
- Any concerns about making this data public? (It's all derived from
  already-public sources, but I want to be thoughtful about
  presentation.)
- Thoughts on where this should live — subdirectory of
  community.apache.org? Separate vhost? Somewhere else?

Looking forward to your thoughts.

Rich
