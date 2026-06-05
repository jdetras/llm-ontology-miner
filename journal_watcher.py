#!/usr/bin/env python3
"""
journal_watcher.py — Scheduled journal scanner for ontology term discovery

Reads journals.yml, polls CrossRef for new papers published since the last
scan, deduplicates against scan history, and feeds each new paper to
ontology_agent.py for agentic term mining.

Usage:
    python journal_watcher.py              # scan now using journals.yml
    python journal_watcher.py --dry-run    # show what would be processed, no LLM calls
    python journal_watcher.py --cron       # print crontab line for automated scheduling
    python journal_watcher.py --status     # show scan history summary
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT         = Path(__file__).parent
HISTORY_FILE = ROOT / "ontologies" / "scan-history.json"
JOURNALS_FILE = ROOT / "journals.yml"

MAX_FAILURES = 3  # stop retrying a DOI after this many agent failures
VALID_FOCUS  = {"po", "to", "both"}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Scheduled journal scanner for ontology term discovery")
    p.add_argument("--dry-run", action="store_true", help="Show what would be processed without making LLM calls")
    p.add_argument("--cron",    action="store_true", help="Print example crontab entries and exit")
    p.add_argument("--status",  action="store_true", help="Show scan history summary and exit")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> tuple[list, dict]:
    if not JOURNALS_FILE.exists():
        print("journals.yml not found. Copy journals.example.yml → journals.yml and customize it.", file=sys.stderr)
        sys.exit(1)

    with JOURNALS_FILE.open() as f:
        config = yaml.safe_load(f)

    raw_journals = config.get("journals") or []
    scan_cfg     = config.get("scan") or {}

    journals = []
    for idx, j in enumerate(raw_journals):
        if not j.get("issn"):
            print(f"journals.yml: entry #{idx + 1} (\"{j.get('name', 'unnamed')}\") is missing 'issn' — skipped")
            continue
        if j.get("focus") and j["focus"] not in VALID_FOCUS:
            print(f"journals.yml: \"{j.get('name', j['issn'])}\" has invalid focus \"{j['focus']}\" (must be po, to, or both) — defaulting to 'both'")
            j["focus"] = "both"
        journals.append(j)

    return journals, scan_cfg

# ---------------------------------------------------------------------------
# Scan history
# ---------------------------------------------------------------------------

def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"last_scan": None, "processed_dois": {}, "failed_dois": {}}
    try:
        h = json.loads(HISTORY_FILE.read_text())
        h.setdefault("failed_dois", {})
        return h
    except Exception:
        return {"last_scan": None, "processed_dois": {}, "failed_dois": {}}


def save_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def mark_processed(history: dict, doi: str) -> None:
    history["processed_dois"][doi.lower()] = datetime.now(timezone.utc).isoformat()


def mark_failed(history: dict, doi: str) -> None:
    key = doi.lower()
    history["failed_dois"][key] = history["failed_dois"].get(key, 0) + 1


def is_processed(history: dict, doi: str) -> bool:
    return doi.lower() in history["processed_dois"]


def is_blocked(history: dict, doi: str) -> bool:
    return history["failed_dois"].get(doi.lower(), 0) >= MAX_FAILURES

# ---------------------------------------------------------------------------
# CrossRef polling
# ---------------------------------------------------------------------------

def fetch_new_papers(journal: dict, lookback_days: int, max_per_journal: int) -> list:
    from_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    params = {
        "filter":  f"issn:{journal['issn']},from-pub-date:{from_date}",
        "sort":    "published",
        "order":   "desc",
        "rows":    max_per_journal,
        "select":  "DOI,title,published,abstract,author,container-title",
    }
    r = requests.get(
        "https://api.crossref.org/works",
        params=params,
        headers={"User-Agent": "journal-watcher/1.0 (mailto:research@example.com)"},
        timeout=30,
    )
    if not r.ok:
        print(f"  CrossRef {r.status_code} for {journal.get('name', '')} ({journal['issn']})")
        return []

    items = r.json().get("message", {}).get("items") or []
    return [
        {
            "doi":         item["DOI"],
            "title":       (item.get("title") or ["(no title)"])[0],
            "journal":     (item.get("container-title") or [journal.get("name", "")])[0],
            "year":        ((item.get("published") or {}).get("date-parts") or [[""]])[0][0],
            "has_abstract": bool(item.get("abstract")),
        }
        for item in items
    ]

# ---------------------------------------------------------------------------
# Spawn agent
# ---------------------------------------------------------------------------

def run_agent(doi: str, journal_ontology: str | None, provider: str, ontology: str, model_flags: list, dry_run: bool) -> None:
    target = journal_ontology or ontology
    agent_args = [sys.executable, str(ROOT / "ontology_agent.py"), "--doi", doi, "--provider", provider, "--ontology", target, *model_flags]
    flags_str = (" " + " ".join(model_flags)) if model_flags else ""
    print(f"    python ontology_agent.py --doi {doi} --provider {provider} --ontology {target}{flags_str}")
    if dry_run:
        return
    result = subprocess.run(agent_args, cwd=ROOT)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, agent_args)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.cron:
        script = ROOT / "journal_watcher.py"
        log    = ROOT / "ontologies" / "watcher.log"
        print("\n# Add one of these to your crontab (crontab -e):\n")
        print(f"# Every day at 8am:")
        print(f"0 8 * * * python {script} >> {log} 2>&1")
        print(f"\n# Every Monday at 9am:")
        print(f"0 9 * * 1 python {script} >> {log} 2>&1")
        print(f"\n# Every 3 days at 8am:")
        print(f"0 8 */3 * * python {script} >> {log} 2>&1")
        print("\n# Or use Claude Code's /schedule skill for managed scheduling.")
        return

    journals, scan_cfg = load_config()

    lookback_days   = scan_cfg.get("lookback_days", 7)
    max_per_journal = scan_cfg.get("max_per_journal", 20)
    provider        = scan_cfg.get("provider") or os.getenv("LLM_PROVIDER", "anthropic")
    ontology        = scan_cfg.get("ontology", "both")
    model           = scan_cfg.get("model")
    model_flags     = ["--model", model] if model else []

    if args.status:
        history = load_history()
        total   = len(history["processed_dois"])
        print(f"Scan history: {HISTORY_FILE}")
        print(f"Last scan   : {history.get('last_scan') or 'never'}")
        print(f"Total DOIs  : {total}")
        if total:
            recent = sorted(history["processed_dois"].items(), key=lambda x: x[1], reverse=True)[:5]
            print("\nMost recent:")
            for doi, ts in recent:
                print(f"  {ts[:10]}  {doi}")
        print(f"\nJournals configured: {len(journals)}")
        for j in journals:
            print(f"  {j['issn']}  {j.get('name', '')}")
        return

    history    = load_history()
    start_time = datetime.now(timezone.utc)

    dry_tag = "[DRY RUN] " if args.dry_run else ""
    print(f"journal-watcher — {dry_tag}scanning {len(journals)} journal(s)")
    print(f"Lookback: {lookback_days} days  |  Provider: {provider}  |  Ontology: {ontology}\n")

    total_new       = 0
    total_processed = 0

    for journal in journals:
        print(f"Scanning: {journal.get('name', journal['issn'])} ({journal['issn']}) ... ", end="", flush=True)
        try:
            papers = fetch_new_papers(journal, lookback_days, max_per_journal)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        new_papers = [p for p in papers if not is_processed(history, p["doi"]) and not is_blocked(history, p["doi"])]
        print(f"{len(papers)} recent, {len(new_papers)} new")
        total_new += len(new_papers)

        for paper in new_papers:
            print(f"\n  DOI   : {paper['doi']}")
            print(f"  Title : {paper['title']}")
            print(f"  {'✓ abstract available' if paper['has_abstract'] else '⚠ no abstract — title only'}")

            try:
                run_agent(paper["doi"], journal.get("focus"), provider, ontology, model_flags, args.dry_run)
                if not args.dry_run:
                    mark_processed(history, paper["doi"])
                    total_processed += 1
            except Exception as e:
                print(f"  ⚠ Agent failed: {e}")
                if not args.dry_run:
                    mark_failed(history, paper["doi"])
                    failures = history["failed_dois"].get(paper["doi"].lower(), 0)
                    if failures >= MAX_FAILURES:
                        print(f"  ✗ Blocked after {MAX_FAILURES} failures — will not retry")

            # Save after each paper so progress isn't lost on error
            history["last_scan"] = datetime.now(timezone.utc).isoformat()
            save_history(history)

    history["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_history(history)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"\n{'─' * 60}")
    print(f"Scan complete in {elapsed:.1f}s")
    print(f"New papers found : {total_new}")
    if args.dry_run:
        print(f"Would have processed: {total_new} (dry run — no LLM calls made)")
    else:
        print(f"Processed          : {total_processed}")
    print(f"History saved    : {HISTORY_FILE}")
    print("\nRun with --status to see history. Use --cron to set up automated scheduling.")


if __name__ == "__main__":
    main()
