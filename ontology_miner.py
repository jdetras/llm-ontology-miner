#!/usr/bin/env python3
"""
ontology_miner.py — Extract candidate ontology terms from plant/trait literature

Single-pass LLM analysis: reads a publication via DOI or text and surfaces
candidate terms for the Plant Ontology (PO) or Trait Ontology (TO).

Usage:
    python ontology_miner.py --doi 10.1093/jxb/eraa002
    python ontology_miner.py --text "paste abstract here"
    python ontology_miner.py --file ./papers/my-paper.txt
    python ontology_miner.py --doi 10.xxxx/xxx --ontology to --provider openai
    python ontology_miner.py --providers
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from providers import PROVIDERS, call_plain_llm, validate_provider

load_dotenv()

ROOT    = Path(__file__).parent
OUT_DIR = ROOT / "ontologies" / "candidates"

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Extract candidate ontology terms from plant science publications")
    p.add_argument("--doi",       help="DOI of the publication to analyze")
    p.add_argument("--text",      help="Publication text to analyze directly")
    p.add_argument("--file",      help="Path to a text file to analyze")
    p.add_argument("--ontology",  default="both", choices=["po", "to", "both"])
    p.add_argument("--provider",  default=os.getenv("LLM_PROVIDER", "anthropic"))
    p.add_argument("--model",     help="Override the default model for the provider")
    p.add_argument("--providers", action="store_true", help="List available providers and exit")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Publication fetch
# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": "ontology-miner/1.0 (mailto:research@example.com)"}


def fetch_by_doi(doi: str) -> dict:
    print(f"Fetching metadata for DOI: {doi}")
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", headers=HEADERS, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as e:
        print(f"Error: CrossRef returned {e.response.status_code} for DOI {doi}.", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"Error: could not reach CrossRef: {e}", file=sys.stderr)
        sys.exit(1)
    work = r.json()["message"]

    title    = (work.get("title") or ["(no title)"])[0]
    authors  = ", ".join(f"{a.get('given', '')} {a.get('family', '')}".strip() for a in work.get("author", []))
    journal  = (work.get("container-title") or [""])[0]
    year     = ((work.get("published") or {}).get("date-parts") or [[""]])[0][0]
    abstract = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", work.get("abstract", ""))).strip()

    if not abstract:
        print("  No abstract in CrossRef — trying Europe PMC...")
        pmc = requests.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"doi:{doi}", "resultType": "core", "format": "json"},
            timeout=30,
        )
        if pmc.ok:
            results = ((pmc.json().get("resultList") or {}).get("result") or [{}])
            abstract = results[0].get("abstractText", "")

    if not abstract:
        print("  Warning: no abstract found — LLM will work from title only.")
        print("  For better results, use --text to paste the full abstract.")

    parts = [f"Title: {title}"]
    if authors: parts.append(f"Authors: {authors}")
    if journal: parts.append(f"Journal: {journal}" + (f" ({year})" if year else ""))
    parts.append(f"\nAbstract:\n{abstract}" if abstract else "\n(No abstract available — title only)")

    return {"source": "doi", "doi": doi, "title": title, "authors": authors, "journal": journal, "year": year, "text": "\n".join(parts)}


def get_publication(args) -> dict:
    if args.doi:
        return fetch_by_doi(args.doi)
    if args.file:
        p = Path(args.file)
        if not p.exists():
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        return {"source": "file", "file": args.file, "text": p.read_text()}
    return {"source": "text", "text": args.text}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ONTOLOGY_CONTEXT = {
    "po": """The Plant Ontology (PO) covers plant anatomy (structures: leaf, root, stem, epidermis),
morphology (shapes, surfaces, arrangements), and developmental stages (germination,
flowering, senescence). Terms describe physical structures or temporal stages.
Examples: "abaxial epidermis", "lateral root cap", "inflorescence meristem", "seed germination stage".""",

    "to": """The Trait Ontology (TO) covers measurable or observable plant traits and phenotypes:
yield traits, stress tolerance, morphological, biochemical, and agronomic traits.
Terms describe heritable characteristics.
Examples: "drought tolerance", "leaf rolling", "tiller number", "grain protein content".""",

    "both": """The Plant Ontology (PO) covers plant anatomy (structures: leaf, root, stem),
morphology (shapes, surfaces), and developmental stages. The Trait Ontology (TO)
covers measurable/observable plant traits and phenotypes: yield, stress tolerance,
morphological, biochemical, and agronomic traits.""",
}

TARGET_LABEL = {
    "both": "Plant Ontology (PO) and/or Trait Ontology (TO)",
    "po":   "Plant Ontology (PO)",
    "to":   "Trait Ontology (TO)",
}


def build_prompts(text: str, ontology: str) -> tuple[str, str]:
    system = f"""You are an expert plant biologist and ontology curator with deep knowledge of
the Plant Ontology (PO) and Trait Ontology (TO). You help curators identify candidate
terms from scientific literature that could enrich these ontologies.

{ONTOLOGY_CONTEXT[ontology]}

When identifying candidate terms:
- Focus on specific, well-defined biological concepts (not vague or generic terms)
- Prefer terms that name a distinct entity, structure, stage, or trait not yet captured
- Include terms used consistently in the paper (not one-off descriptors)
- Synonyms are valuable — note them alongside the primary term
- A term that refines an existing concept (a narrower child term) is as valuable as a new branch"""

    user = f"""Analyze this plant science publication and identify candidate terms for {TARGET_LABEL[ontology]}.

PUBLICATION:
{text}

For each candidate term, return a JSON object with:
- "term": the candidate term name (noun phrase, lowercase)
- "ontology": "PO" | "TO" | "either"
- "namespace": (PO) "plant_anatomy" | "plant_morphology" | "plant_developmental_stage"; (TO) "trait"
- "definition_draft": a concise 1-2 sentence definition suitable for an ontology
- "suggested_parent": most likely parent concept in PO or TO (use existing terms if known)
- "synonyms": array of alternate names or abbreviations from the paper
- "source_sentence": the exact sentence(s) from the paper where this term appears
- "rationale": 1-2 sentences explaining why this is a good ontology candidate
- "confidence": "high" | "medium" | "low"

Return ONLY a JSON array. If no strong candidates exist, return [].
Do not include generic terms already well-established in PO/TO."""

    return system, user

# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(raw: str) -> list:
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = 0
    while True:
        i = raw.find("[", start)
        if i == -1:
            break
        depth = 0
        for j in range(i, len(raw)):
            if raw[j] == "[":
                depth += 1
            elif raw[j] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[i:j + 1])
                    except json.JSONDecodeError:
                        break
        start = i + 1
    return None

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", s.lower()))[:40]


def print_summary(candidates: list) -> None:
    by_conf: dict[str, list] = {"high": [], "medium": [], "low": []}
    for c in candidates:
        by_conf.get(c.get("confidence", "low"), by_conf["low"]).append(c)

    print(f"\n{'─' * 60}")
    print(f"Candidate terms found: {len(candidates)}  (high: {len(by_conf['high'])}  medium: {len(by_conf['medium'])}  low: {len(by_conf['low'])})")
    print("─" * 60)

    for c in by_conf["high"] + by_conf["medium"] + by_conf["low"]:
        badge = {"high": "●", "medium": "◐", "low": "○"}.get(c.get("confidence"), "○")
        print(f"\n{badge} [{c.get('ontology', '?')}] {c.get('term', '')}")
        if c.get("namespace"):        print(f"  Namespace : {c['namespace']}")
        if c.get("suggested_parent"): print(f"  Parent    : {c['suggested_parent']}")
        if c.get("definition_draft"): print(f"  Def       : {c['definition_draft']}")
        if c.get("synonyms"):         print(f"  Synonyms  : {', '.join(c['synonyms'])}")
        if c.get("rationale"):        print(f"  Rationale : {c['rationale']}")
    print("\n" + "─" * 60)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()

    if args.providers:
        print("Available providers:\n")
        for pid, p in PROVIDERS.items():
            key_note = f"env: {p['env_key']}" if p["env_key"] else "no key required"
            print(f"  {pid:<16} {p['label']}")
            print(f"  {'':16} default model: {p['default_model']}  |  {key_note}\n")
        return

    if not any([args.doi, args.text, args.file]):
        print("Usage:", file=sys.stderr)
        print("  python ontology_miner.py --doi 10.xxxx/xxxxx", file=sys.stderr)
        print("  python ontology_miner.py --text \"abstract text\"", file=sys.stderr)
        print("  python ontology_miner.py --file ./paper.txt", file=sys.stderr)
        print("  Flags: --ontology po|to|both  --provider <name>  --model <name>", file=sys.stderr)
        sys.exit(1)

    cfg   = validate_provider(args.provider)
    model = args.model or cfg["default_model"]

    print(f"ontology-miner — candidate term extractor for PO/TO")
    print(f"Provider: {cfg['label']}  |  Model: {model}\n")

    pub = get_publication(args)
    print(f"Source  : {pub['source']}" + (f" ({pub['doi']})" if pub.get("doi") else ""))
    if pub.get("title"): print(f"Title   : {pub['title']}")
    print(f"Ontology: {args.ontology.upper()}\n")

    print("Sending to LLM for analysis...")
    system, user = build_prompts(pub["text"], args.ontology)
    try:
        raw = call_plain_llm(system, user, args.provider, model)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    candidates = extract_json(raw)

    if candidates is None:
        print("\nCould not parse LLM response as JSON. Raw output:\n")
        print(raw)
    elif not candidates:
        print("\nNo candidate terms identified in this publication.")
    else:
        print_summary(candidates)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now  = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    raw_slug = pub.get("doi") or pub.get("title") or Path(pub.get("file", "text")).stem or (pub.get("text", "text"))[:50]
    slug     = slugify(raw_slug)
    out_path = OUT_DIR / f"{slug}-{date}.json"

    out_path.write_text(json.dumps({
        "mined_at":        now.isoformat(),
        "provider":        args.provider,
        "model":           model,
        "source":          pub["source"],
        "doi":             pub.get("doi"),
        "title":           pub.get("title"),
        "ontology_target": args.ontology,
        "candidate_count": len(candidates) if candidates else 0,
        "candidates":      candidates or [],
    }, indent=2))

    print(f"\nSaved → ontologies/candidates/{out_path.name}")


if __name__ == "__main__":
    main()
