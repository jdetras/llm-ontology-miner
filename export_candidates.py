#!/usr/bin/env python3
"""
export_candidates.py — Convert mined candidates to ontology submission formats

Reads a candidates JSON file produced by ontology_miner.py or ontology_agent.py
and exports submission-ready files for ontology curators.

Output formats:
  obo      — OBO stanza blocks, ready to paste into a .obo file or GitHub PR
  robot    — ROBOT template TSV, importable with `robot template`
  github   — GitHub New Term Request (NTR) markdown, one file per term
  csv      — Flat CSV for spreadsheet-based human review
  all      — All of the above (default)

Usage:
    python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json
    python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json --format obo
    python export_candidates.py --list
"""

import argparse
import csv
import io
import json
import sys
from pathlib import Path

ROOT     = Path(__file__).parent
CAND_DIR = ROOT / "ontologies" / "candidates"
EXP_DIR  = ROOT / "ontologies" / "exports"

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Export candidate ontology terms to submission formats")
    p.add_argument("--input",  help="Path to a candidates JSON file")
    p.add_argument("--format", default="all", choices=["obo", "robot", "github", "csv", "all"])
    p.add_argument("--list",   action="store_true", help="List available candidate files and exit")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GITHUB_REPOS = {
    "PO":    "https://github.com/Planteome/plant-ontology/issues/new",
    "TO":    "https://github.com/Planteome/plant-trait-ontology/issues/new",
    "PECO":  "https://github.com/Planteome/plant-experimental-conditions-ontology/issues/new",
    "FLOPO": "https://github.com/flora-phenotype-ontology/flopoontology/issues/new",
}

NAMESPACE_PREFIX = {
    "plant_anatomy":             "PO",
    "plant_morphology":          "PO",
    "plant_developmental_stage": "PO",
    "trait":                     "TO",
}


def term_prefix(c: dict) -> str | None:
    ns = NAMESPACE_PREFIX.get(c.get("namespace", ""))
    if ns:
        return ns
    if c.get("ontology") == "TO":
        return "TO"
    if c.get("ontology") == "PO":
        return "PO"
    return None  # 'either' or unknown — curator must assign namespace


def placeholder_id(c: dict, index: int) -> str:
    prefix = term_prefix(c)
    num    = str(index + 1).zfill(3)
    return f"{prefix}:NEWTERM_{num}" if prefix else f"??:NEWTERM_{num}"


def doi_ref(doi: str | None) -> str:
    return f"DOI:{doi}" if doi else "REF:UNKNOWN"

# ---------------------------------------------------------------------------
# Format: OBO stanzas
# ---------------------------------------------------------------------------

def build_obo(candidates: list, doi: str | None, title: str | None, mined_at: str, input_path: Path) -> str:
    lines = [
        "! OBO stanza export",
        f"! Source   : {input_path}",
        f"! Paper    : {title or '(unknown)'}",
        f"! DOI      : {doi or '(unknown)'}",
        f"! Mined at : {mined_at}",
        "! Note     : Replace NEWTERM_XXX IDs with real accessions before committing.",
        "!            Review and edit definition_draft before committing.",
        "",
    ]
    for i, c in enumerate(candidates):
        pid = placeholder_id(c, i)
        lines.append("[Term]")
        lines.append(f"id: {pid}")
        lines.append(f"name: {c['term']}")
        if c.get("namespace"):
            lines.append(f"namespace: {c['namespace']}")
        if c.get("definition_draft"):
            defn = c["definition_draft"].replace('"', '\\"')
            lines.append(f'def: "{defn}" [{doi_ref(doi)}]')
        if c.get("suggested_parent"):
            lines.append(f"! suggested_parent: {c['suggested_parent']}  (verify PO/TO accession before using is_a)")
        for syn in c.get("synonyms") or []:
            lines.append(f'synonym: "{syn}" EXACT []')
        if c.get("source_sentence"):
            src = c["source_sentence"][:200].replace('"', '\\"')
            lines.append(f'comment: Source: "{src}"')
        lines.append(f"! confidence: {c.get('confidence', 'unknown')}")
        lines.append("")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Format: ROBOT template TSV
# ---------------------------------------------------------------------------

def build_robot(candidates: list, doi: str | None) -> str:
    headers      = ["ID", "Label", "Definition", "Parent", "Exact Synonym", "Comment"]
    robot_header = ["ID", "LABEL", "A definition 'definition'@en", "SC %", "A oboInOwl:hasExactSynonym", "A rdfs:comment"]

    buf = io.StringIO()
    w   = csv.writer(buf, delimiter="\t", quotechar='"', quoting=csv.QUOTE_ALL)
    w.writerow(headers)
    w.writerow(robot_header)

    for i, c in enumerate(candidates):
        comment_parts = [
            c.get("rationale") or "",
            f"Source DOI: {doi}" if doi else "",
            f"Confidence: {c.get('confidence', 'unknown')}",
            f"Evidence: \"{c['source_sentence'][:150]}\"" if c.get("source_sentence") else "",
        ]
        w.writerow([
            placeholder_id(c, i),
            c.get("term", ""),
            c.get("definition_draft", ""),
            c.get("suggested_parent", ""),
            "|".join(c.get("synonyms") or []),
            " | ".join(p for p in comment_parts if p),
        ])

    return buf.getvalue()

# ---------------------------------------------------------------------------
# Format: GitHub NTR issues
# ---------------------------------------------------------------------------

def build_github_issue(c: dict, doi: str | None, title: str | None) -> str:
    is_ambiguous = not c.get("ontology") or c.get("ontology") == "either"
    onto  = c.get("ontology") if c.get("ontology") in ("PO", "TO") else None
    repo  = GITHUB_REPOS.get(onto) if onto else None

    onto_label = onto or "PO or TO (curator: please decide)"
    if onto == "PO":
        onto_full = "Plant Ontology (PO)"
    elif onto == "TO":
        onto_full = "Trait Ontology (TO)"
    else:
        onto_full = None

    lines = [
        f"## New Term Request — {onto_label}",
        "",
        f"**Proposed label:** {c.get('term', '')}",
        f"**Ontology:** {onto_full}" if onto_full else "**Ontology:** ⚠ Ambiguous — curator must determine whether this belongs in PO or TO",
        f"**Namespace:** `{c['namespace']}`" if c.get("namespace") else None,
        "",
        "### Definition",
        "",
        c.get("definition_draft") or "_(draft — please review)_",
        "",
        "### Suggested parent term",
        "",
        f"`{c['suggested_parent']}`" if c.get("suggested_parent") else "_(see rationale)_",
        "",
    ]

    if c.get("synonyms"):
        lines += ["### Synonyms", ""]
        for s in c["synonyms"]:
            lines.append(f"- `{s}` (EXACT)")
        lines.append("")

    lines += [
        "### Evidence",
        "",
        f"**Paper DOI:** {doi}" if doi else None,
        f"**Paper title:** {title}" if title else None,
        "",
        "**Supporting sentence from paper:**",
        "",
        f"> {c.get('source_sentence') or '_(not available)_'}",
        "",
        "### Rationale",
        "",
        c.get("rationale") or "_(auto-generated candidate — please review)_",
        "",
        "### Automated check (OLS)",
        "",
        f"OLS search result: {c['ols_search_result']}" if c.get("ols_search_result") else "_OLS check not recorded_",
        "",
        "---",
        f"_Candidate generated by [llm-ontology-miner](https://github.com/jdetras/llm-ontology-miner) · confidence: {c.get('confidence', 'unknown')}_",
        "",
        (
            f"**Curator note:** Ontology is ambiguous — decide before submitting.\n- [PO Issues]({GITHUB_REPOS['PO']})\n- [TO Issues]({GITHUB_REPOS['TO']})"
            if is_ambiguous
            else f"**Submit to:** [{onto} GitHub Issues]({repo})"
        ),
    ]

    return "\n".join(line for line in lines if line is not None)


def build_github_all(candidates: list, doi: str | None, title: str | None) -> str:
    parts = [f"<!-- TERM {i + 1} of {len(candidates)} -->\n\n{build_github_issue(c, doi, title)}" for i, c in enumerate(candidates)]
    return "\n\n---\n\n".join(parts)

# ---------------------------------------------------------------------------
# Format: CSV (human review)
# ---------------------------------------------------------------------------

def build_csv(candidates: list, doi: str | None) -> str:
    fieldnames = ["#", "confidence", "ontology", "namespace", "term", "definition_draft",
                  "suggested_parent", "synonyms", "source_sentence", "rationale", "ols_check", "doi"]
    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
    w.writeheader()
    for i, c in enumerate(candidates):
        w.writerow({
            "#":               i + 1,
            "confidence":      c.get("confidence", ""),
            "ontology":        c.get("ontology", ""),
            "namespace":       c.get("namespace", ""),
            "term":            c.get("term", ""),
            "definition_draft":c.get("definition_draft", ""),
            "suggested_parent":c.get("suggested_parent", ""),
            "synonyms":        "; ".join(c.get("synonyms") or []),
            "source_sentence": c.get("source_sentence", ""),
            "rationale":       c.get("rationale", ""),
            "ols_check":       c.get("ols_search_result", ""),
            "doi":             doi or "",
        })
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.list:
        if not CAND_DIR.exists():
            print("No candidates directory found.")
            return
        files = sorted([f for f in CAND_DIR.iterdir() if f.suffix == ".json" and f.name != ".gitkeep"], reverse=True)
        if not files:
            print("No candidate files found in ontologies/candidates/")
            return
        print("Candidate files in ontologies/candidates/:\n")
        for f in files:
            data = json.loads(f.read_text())
            print(f"  {f.name}  ({data.get('candidate_count', 0)} candidates)")
        return

    if not args.input:
        print("Usage: python export_candidates.py --input <candidates-json> [--format obo|robot|github|csv|all]", file=sys.stderr)
        print("       python export_candidates.py --list", file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input) if Path(args.input).is_absolute() else ROOT / args.input
    if not input_path.exists():
        print(f"File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    data       = json.loads(input_path.read_text())
    candidates = [c for c in (data.get("candidates") or []) if not c.get("_raw")]
    doi        = data.get("doi")
    title      = data.get("title")
    mined_at   = data.get("mined_at", "")

    if not candidates:
        print("No candidates found in this file.")
        return

    slug     = input_path.stem
    out_base = EXP_DIR / slug
    EXP_DIR.mkdir(parents=True, exist_ok=True)

    print(f"export-candidates — {len(candidates)} candidate(s) from {input_path.name}\n")

    formats = ["obo", "robot", "github", "csv"] if args.format == "all" else [args.format]
    written = []

    for fmt in formats:
        if fmt == "obo":
            content  = build_obo(candidates, doi, title, mined_at, input_path)
            ext, label = ".obo", "OBO stanzas"
        elif fmt == "robot":
            content  = build_robot(candidates, doi)
            ext, label = "-robot.tsv", "ROBOT template TSV"
        elif fmt == "github":
            content  = build_github_all(candidates, doi, title)
            ext, label = "-ntr.md", "GitHub NTR markdown"
        elif fmt == "csv":
            content  = build_csv(candidates, doi)
            ext, label = ".csv", "CSV for review"
        else:
            print(f"Unknown format: {fmt}", file=sys.stderr)
            continue

        out_path = out_base.parent / f"{out_base.name}{ext}"
        out_path.write_text(content)
        written.append((label, out_path))
        print(f"✓ {label:<22} → ontologies/exports/{out_path.name}")

    print(f"\n{len(candidates)} candidate(s) exported in {len(written)} format(s).")

    if "github" in formats:
        ontos = sorted({
            onto
            for c in candidates
            for onto in (["PO", "TO"] if not c.get("ontology") or c.get("ontology") == "either" else [c["ontology"]])
            if onto in GITHUB_REPOS
        })
        print("\nSubmission links:")
        for onto in ontos:
            print(f"  {onto}: {GITHUB_REPOS[onto]}")
        ambiguous = [c for c in candidates if not c.get("ontology") or c.get("ontology") == "either"]
        if ambiguous:
            print(f"\n  ⚠ {len(ambiguous)} candidate(s) have ambiguous ontology — see curator note in the NTR file")
        print("\nTip: open the -ntr.md file, copy each term block, and paste into a new GitHub issue.")

    if "robot" in formats:
        print("\nROBOT import command:")
        print(f"  robot template --input your-ontology.owl --template {out_base}-robot.tsv --output new-terms.owl")


if __name__ == "__main__":
    main()
