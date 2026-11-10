"""
Job market tracker.

Each run:
  1. Fetches postings from Adzuna for each configured search.
  2. Skips jobs already in the SQLite database (idempotency).
  3. Scores new jobs against your resume and criteria using Claude.
  4. Writes a Markdown digest of jobs above the score threshold.
  5. Logs everything to logs/ as JSON lines.

Setup:
  pip install -r requirements.txt
  export ADZUNA_APP_ID=...
  export ADZUNA_APP_KEY=...
  export ANTHROPIC_API_KEY=...
  cp config.example.json config.json   # then edit
  python tracker.py --config config.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()  # reads .env in the project folder, if present

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
DIGEST_DIR = BASE_DIR / "digests"


# ---------- logging ----------

def log_event(run_id: str, event: str, **fields):
    """Append one JSON line per event so runs can be reviewed later."""
    LOG_DIR.mkdir(exist_ok=True)
    record = {"ts": dt.datetime.now().isoformat(), "run_id": run_id, "event": event, **fields}
    log_file = LOG_DIR / f"{dt.date.today().isoformat()}.jsonl"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- storage (idempotency) ----------

def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            first_seen TEXT,
            score INTEGER
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute("SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, job: dict, score: int | None):
    conn.execute(
        "INSERT OR REPLACE INTO seen_jobs (job_id, title, company, first_seen, score) VALUES (?, ?, ?, ?, ?)",
        (job["job_id"], job["title"], job["company"], dt.datetime.now().isoformat(), score),
    )
    conn.commit()


# ---------- fetching ----------

def fetch_adzuna(search: dict, cfg: dict) -> list[dict]:
    """Return normalized job dicts from Adzuna for one search."""
    app_id = os.environ["ADZUNA_APP_ID"]
    app_key = os.environ["ADZUNA_APP_KEY"]
    country = cfg["adzuna"]["country"]
    per_page = cfg["adzuna"]["results_per_page"]
    jobs = []

    for page in range(1, cfg["adzuna"]["max_pages"] + 1):
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": per_page,
            "what": search["what"],
            "max_days_old": cfg["max_days_old"],
            "content-type": "application/json",
        }
        if search.get("where"):  # location is optional; omit it for nationwide results
            params["where"] = search["where"]
        url = (
            f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?"
            + urllib.parse.urlencode(params)
        )
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        for r in results:
            jobs.append(
                {
                    "job_id": f"adzuna:{r['id']}",
                    "title": r.get("title", "").strip(),
                    "company": (r.get("company") or {}).get("display_name", "Unknown"),
                    "location": (r.get("location") or {}).get("display_name", ""),
                    "description": r.get("description", ""),
                    "url": r.get("redirect_url", ""),
                    "salary_min": r.get("salary_min"),
                    "salary_max": r.get("salary_max"),
                    "created": r.get("created", ""),
                }
            )
        if len(results) < per_page:
            break  # no more pages

    return jobs


# ---------- scoring ----------

SCORING_INSTRUCTIONS = """You evaluate job postings for a candidate.

You will receive the candidate's resume, their search criteria, and one job posting.
Return ONLY a JSON object, with no preamble and no Markdown fences, in this shape:

{
  "score": <integer 0-100>,
  "summary": "<one or two sentences on fit>",
  "matches": ["<strongest overlaps with the resume>"],
  "gaps": ["<requirements the candidate appears to lack>"],
  "red_flags": ["<anything concerning in the posting, such as vague scope or unrealistic requirements>"]
}

Scoring guide:
- 85-100: strong match on role, skills, and stated criteria
- 70-84: good match with minor gaps
- 50-69: partial match, worth a look only if other factors are attractive
- 0-49: poor match or violates a hard criterion

Keep the output short: the summary should be under 40 words, and each list should have at most 4 items. Output must be complete valid JSON.

Base the score only on the information provided. If the description is thin, say so in red_flags and score conservatively."""


def score_job(client: anthropic.Anthropic, model: str, resume: str, criteria: str, job: dict) -> dict:
    job_text = (
        f"Title: {job['title']}\n"
        f"Company: {job['company']}\n"
        f"Location: {job['location']}\n"
        f"Salary: {job.get('salary_min')} to {job.get('salary_max')}\n\n"
        f"Description:\n{job['description']}"
    )
    user_content = (
        f"<resume>\n{resume}\n</resume>\n\n"
        f"<criteria>\n{criteria}\n</criteria>\n\n"
        f"<job>\n{job_text}\n</job>"
    )
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        system=SCORING_INSTRUCTIONS,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # Strip accidental code fences, then parse. Raise if it's still not valid JSON.
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ---------- digest ----------

def write_digest(entries: list[dict], path: Path):
    """entries: list of {job, result} sorted by score desc."""
    DIGEST_DIR.mkdir(exist_ok=True)
    lines = [f"# Job digest {dt.date.today().isoformat()}", ""]
    if not entries:
        lines.append("No new jobs met the score threshold today.")
    for e in entries:
        job, res = e["job"], e["result"]
        lines.append(f"## {job['title']} at {job['company']} ({res['score']}/100)")
        lines.append(f"- Location: {job['location']}")
        lines.append(f"- Link: {job['url']}")
        lines.append(f"- Fit: {res.get('summary', '')}")
        if res.get("matches"):
            lines.append(f"- Matches: {', '.join(res['matches'])}")
        if res.get("gaps"):
            lines.append(f"- Gaps: {', '.join(res['gaps'])}")
        if res.get("red_flags"):
            lines.append(f"- Red flags: {', '.join(res['red_flags'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------- main ----------

def run(config_path: str):
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_event(run_id, "run_start", searches=len(cfg["searches"]))

    resume = Path(cfg["resume_path"]).read_text(encoding="utf-8")
    criteria = Path(cfg["criteria_path"]).read_text(encoding="utf-8")
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    conn = open_db(cfg["db_path"])

    kept = []
    stats = {"fetched": 0, "new": 0, "scored": 0, "kept": 0, "errors": 0}

    for search in cfg["searches"]:
        try:
            jobs = fetch_adzuna(search, cfg)
        except Exception as e:
            stats["errors"] += 1
            log_event(run_id, "fetch_error", search=search, error=str(e))
            continue

        stats["fetched"] += len(jobs)
        for job in jobs:
            if is_seen(conn, job["job_id"]):
                continue
            stats["new"] += 1
            try:
                result = score_job(client, cfg["model"], resume, criteria, job)
                score = int(result["score"])
                stats["scored"] += 1
                log_event(run_id, "scored", job_id=job["job_id"], title=job["title"],
                          company=job["company"], score=score)
                mark_seen(conn, job, score)
                if score >= cfg["score_threshold"]:
                    kept.append({"job": job, "result": result})
                    stats["kept"] += 1
            except Exception as e:
                # Don't mark as seen, so the job is retried on the next run.
                stats["errors"] += 1
                log_event(run_id, "score_error", job_id=job["job_id"], error=str(e))

    kept.sort(key=lambda e: e["result"]["score"], reverse=True)
    digest_path = DIGEST_DIR / f"{dt.date.today().isoformat()}.md"
    write_digest(kept, digest_path)
    log_event(run_id, "run_end", digest=str(digest_path), **stats)
    conn.close()
    print(f"Digest written to {digest_path}. Stats: {stats}")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    try:
        stats = run(args.config)
        # Exit non-zero if everything failed, so the scheduler can alert you.
        if stats["errors"] and stats["scored"] == 0 and stats["fetched"] == 0:
            sys.exit(1)
    except Exception:
        run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_event(run_id, "fatal", traceback=traceback.format_exc())
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()