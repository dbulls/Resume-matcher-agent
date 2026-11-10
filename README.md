# Job Market Tracker

A scheduled agent that pulls job postings from Adzuna, scores them against your resume using Claude Sonnet 5.5, and writes a daily Markdown digest of the strongest matches.

## Project layout

```
job-tracker/
├── tracker.py           # main script
├── config.example.json  # copy to config.json and edit
├── requirements.txt
├── data/
│   ├── resume.md        # your resume as plain text or Markdown
│   ├── criteria.md      # hard requirements and preferences
│   └── seen_jobs.db     # created automatically
├── digests/             # daily Markdown output
└── logs/                # JSON lines, one file per day
```

## Setup

1. Get API credentials:
   - Adzuna: free developer account at developer.adzuna.com (provides an app ID and key)
   - Anthropic: your existing API key
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set environment variables:
   ```bash
   export ADZUNA_APP_ID=your_id
   export ADZUNA_APP_KEY=your_key
   export ANTHROPIC_API_KEY=your_key
   ```
4. Create your config and inputs:
   ```bash
   cp config.example.json config.json
   ```
   Then write `data/resume.md` and `data/criteria.md`. In `criteria.md`, list hard requirements (for example, "must be onsite or hybrid in Michigan," "no clearance required") and soft preferences (for example, "prefer Python-heavy roles").
5. Run once manually to check the output:
   ```bash
   python tracker.py --config config.json
   ```

## Scheduling

Cron (macOS/Linux), runs daily at 7:00 AM:

```
0 7 * * * cd /path/to/job-tracker && /usr/bin/python3 tracker.py --config config.json >> logs/cron.log 2>&1
```

The script exits non-zero when a run fails outright, so you can wire up an alert from cron or a GitHub Actions workflow later.

## How it avoids duplicates

Every job gets a stable ID (`adzuna:<id>`) stored in SQLite. Reruns skip anything already seen. Jobs that error during scoring are not marked as seen, so they get retried on the next run.

## Next steps

- Add a notification step (email or a Discord/Slack webhook) when a digest is written or a run fails.
- Add USAJobs as a second source.
- Add a feedback loop: mark jobs as "applied" or "not interested" and use those labels to refine the scoring prompt.
