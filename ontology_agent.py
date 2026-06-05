#!/usr/bin/env python3
"""
ontology_agent.py — Agentic ontology term miner with tool use

Multi-step agent: proposes candidates, validates each against the EBI OLS API,
optionally fetches cited papers for context, then self-critiques before saving.

Tool use is supported for: anthropic, openai, gemini, mistral, ollama (llama3.2+),
openai-compat (if the endpoint supports function calling).

Usage:
    python ontology_agent.py --doi 10.1093/jxb/eraa002
    python ontology_agent.py --text "paste abstract here"
    python ontology_agent.py --file ./papers/paper.txt
    python ontology_agent.py --doi 10.xxxx/xxx --ontology to --provider openai
    python ontology_agent.py --providers
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from providers import PROVIDERS, _fmt_request_error, call_plain_llm, resolve_provider_base, validate_provider

load_dotenv()

ROOT          = Path(__file__).parent
OUT_DIR       = ROOT / "ontologies" / "candidates"
MAX_TOOL_ROUNDS = 10

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Agentic ontology term miner with OLS validation")
    p.add_argument("--doi",       help="DOI of the publication to analyze")
    p.add_argument("--text",      help="Publication text to analyze directly")
    p.add_argument("--file",      help="Path to a text file to analyze")
    p.add_argument("--ontology",  default="both", choices=["po", "to", "both"])
    p.add_argument("--provider",  default=os.getenv("LLM_PROVIDER", "anthropic"))
    p.add_argument("--model",     help="Override the default model for the provider")
    p.add_argument("--providers", action="store_true", help="List available providers and exit")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Publication fetch (same logic as ontology_miner.py)
# ---------------------------------------------------------------------------

FETCH_HEADERS = {"User-Agent": "ontology-agent/1.0 (mailto:research@example.com)"}


def fetch_by_doi(doi: str) -> dict:
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", headers=FETCH_HEADERS, timeout=30)
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
        pmc = requests.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"doi:{doi}", "resultType": "core", "format": "json"},
            timeout=30,
        )
        if pmc.ok:
            abstract = (((pmc.json().get("resultList") or {}).get("result") or [{}])[0]).get("abstractText", "")

    if not abstract:
        print("  Warning: no abstract found — agent will work from title only.")

    parts = [f"Title: {title}"]
    if authors: parts.append(f"Authors: {authors}")
    if journal: parts.append(f"Journal: {journal}" + (f" ({year})" if year else ""))
    parts.append(f"\nAbstract:\n{abstract}" if abstract else "\n(No abstract available)")

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
# Tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "name": "search_ontology",
        "description": (
            "Search the Plant Ontology (PO) or Trait Ontology (TO) via the EBI OLS API to check "
            "whether a candidate term already exists. Call this for every candidate before proposing "
            "it — only surface genuinely novel terms."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "term":     {"type": "string", "description": "The term or phrase to search for"},
                "ontology": {"type": "string", "enum": ["po", "to", "both"], "description": "Which ontology to search"},
            },
            "required": ["term", "ontology"],
        },
    },
    {
        "name": "fetch_abstract",
        "description": (
            "Fetch the title and abstract of a related paper by DOI. Use this when the main paper "
            "cites a closely related work that might clarify a candidate term."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doi": {"type": "string", "description": "DOI of the paper to fetch"},
            },
            "required": ["doi"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

def search_ontology(term: str, onto: str) -> dict:
    onto_param = "po,to" if onto == "both" else onto
    url = (
        f"https://www.ebi.ac.uk/ols4/api/search"
        f"?q={quote(term)}&ontology={onto_param}&type=class&rows=5&exact=false&lang=en"
    )
    try:
        res = requests.get(url, timeout=20)
        if res.status_code == 429:
            return {"found": None, "error": "OLS rate limited (429) — result unverified, do not treat as absent"}
        if not res.ok:
            return {"found": False, "error": f"OLS API {res.status_code}"}
        hits = res.json().get("response", {}).get("docs", [])
        if not hits:
            return {"found": False, "message": f'No existing terms match "{term}" in {onto.upper()}'}
        return {
            "found": True,
            "message": f"Found {len(hits)} potential match(es) — review before proposing as new:",
            "matches": [
                {
                    "id":         h.get("obo_id") or h.get("id"),
                    "label":      h.get("label"),
                    "ontology":   h.get("ontology_name"),
                    "definition": (h.get("description") or [""])[0][:120],
                }
                for h in hits
            ],
        }
    except Exception as e:
        return {"found": None, "error": f"Search failed (network/parse error): {e} — result unverified, do not treat as absent"}


def execute_tool(name: str, inputs: dict) -> dict:
    print(f"  → tool: {name}({json.dumps(inputs)})")
    if name == "search_ontology":
        return search_ontology(inputs["term"], inputs.get("ontology", "both"))
    if name == "fetch_abstract":
        try:
            pub = fetch_by_doi(inputs["doi"])
            return {"title": pub.get("title"), "text": pub["text"]}
        except Exception as e:
            return {"error": str(e)}
    return {"error": f"Unknown tool: {name}"}

# ---------------------------------------------------------------------------
# Provider-specific tool format converters
# ---------------------------------------------------------------------------

def _to_anthropic_tools(defs: list) -> list:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in defs]


def _to_openai_tools(defs: list) -> list:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in defs]


def _to_gemini_tools(defs: list) -> list:
    return [{"function_declarations": [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in defs]}]

# ---------------------------------------------------------------------------
# Agentic loops per provider
# ---------------------------------------------------------------------------

def _run_agent_anthropic(system: str, user: str, model: str) -> str:
    messages = [{"role": "user", "content": user}]
    tools    = _to_anthropic_tools(TOOL_DEFS)

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": model, "max_tokens": 4096, "system": system, "tools": tools, "messages": messages},
                timeout=120,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(_fmt_request_error(e, "Anthropic")) from e
        data      = r.json()
        content   = data.get("content", [])
        tool_uses = [c for c in content if c.get("type") == "tool_use"]

        if not tool_uses:
            return "".join(c["text"] for c in content if c.get("type") == "text")

        messages.append({"role": "assistant", "content": content})
        results = [
            {"type": "tool_result", "tool_use_id": t["id"], "content": json.dumps(execute_tool(t["name"], t["input"]))}
            for t in tool_uses
        ]
        messages.append({"role": "user", "content": results})

    raise RuntimeError("Agent loop exceeded max rounds")


def _run_agent_openai_compat(system: str, user: str, base_url: str, api_key: str | None, model: str) -> str:
    messages  = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    tools     = _to_openai_tools(TOOL_DEFS)
    headers   = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for _ in range(MAX_TOOL_ROUNDS):
        payload: dict = {"model": model, "max_tokens": 4096, "messages": messages, "tools": tools, "tool_choice": "auto"}
        try:
            r = requests.post(
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(_fmt_request_error(e, base_url)) from e
        choice = r.json().get("choices", [{}])[0]

        finish = choice.get("finish_reason")
        if finish == "length":
            raise RuntimeError("Model response truncated (finish_reason=length) — increase max_tokens or shorten input")
        if finish != "tool_calls":
            return choice.get("message", {}).get("content") or ""

        messages.append(choice["message"])
        for tc in choice["message"].get("tool_calls", []):
            fn      = tc["function"]
            result  = execute_tool(fn["name"], json.loads(fn["arguments"]))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})

    raise RuntimeError("Agent loop exceeded max rounds")


def _run_agent_gemini(system: str, user: str, model: str) -> str:
    contents = [{"role": "user", "parts": [{"text": user}]}]
    tools    = _to_gemini_tools(TOOL_DEFS)

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model)}:generateContent",
                params={"key": os.environ["GEMINI_API_KEY"]},
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": contents,
                    "tools":    tools,
                    "generationConfig": {"maxOutputTokens": 4096},
                },
                timeout=120,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(_fmt_request_error(e, "Gemini")) from e
        parts      = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        func_calls = [p for p in parts if "functionCall" in p]

        if not func_calls:
            return "".join(p["text"] for p in parts if "text" in p)

        contents.append({"role": "model", "parts": parts})
        responses = [
            {"functionResponse": {"name": p["functionCall"]["name"], "response": execute_tool(p["functionCall"]["name"], p["functionCall"]["args"])}}
            for p in func_calls
        ]
        # Gemini requires role:'user' for function responses (not 'function')
        contents.append({"role": "user", "parts": responses})

    raise RuntimeError("Agent loop exceeded max rounds")


def run_agent(system: str, user: str, provider_key: str, model: str) -> str:
    if provider_key == "anthropic":
        return _run_agent_anthropic(system, user, model)
    if provider_key == "gemini":
        return _run_agent_gemini(system, user, model)

    base_url, api_key = resolve_provider_base(provider_key)
    return _run_agent_openai_compat(system, user, base_url, api_key, model)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ONTOLOGY_CONTEXT = {
    "po":   "The Plant Ontology (PO) covers plant anatomy (leaf, root, stem, epidermis), morphology (shapes, surfaces), and developmental stages (germination, flowering, senescence).",
    "to":   "The Trait Ontology (TO) covers measurable or observable plant traits: yield, stress tolerance, morphological, biochemical, and agronomic traits.",
    "both": "PO covers plant anatomy, morphology, and developmental stages. TO covers measurable plant traits and phenotypes.",
}

TARGET_LABEL = {
    "both": "Plant Ontology (PO) and/or Trait Ontology (TO)",
    "po":   "Plant Ontology (PO)",
    "to":   "Trait Ontology (TO)",
}


def build_mining_prompt(text: str, ontology: str) -> tuple[str, str]:
    system = f"""You are an expert plant biologist and ontology curator. Your task is to identify candidate terms from scientific literature that could enrich {TARGET_LABEL[ontology]}.

{ONTOLOGY_CONTEXT[ontology]}

WORKFLOW — follow this exactly:
1. Read the publication and identify candidate terms.
2. For EACH candidate, call search_ontology to check if it already exists. Only propose terms that are genuinely new.
3. Use fetch_abstract for cited DOIs if they would help clarify a term's definition or scope (max 3 calls).
4. After validating all candidates, return a JSON array.

Each candidate object must have:
- "term": noun phrase, lowercase
- "ontology": "PO" | "TO" | "either"
- "namespace": "plant_anatomy" | "plant_morphology" | "plant_developmental_stage" | "trait"
- "definition_draft": 1-2 sentence definition
- "suggested_parent": closest existing parent term in PO/TO
- "synonyms": array of alternate names from the paper
- "source_sentence": exact sentence from the paper
- "rationale": why this is a good ontology candidate
- "confidence": "high" | "medium" | "low"
- "ols_search_result": brief summary of what search_ontology returned (confirms you checked)

Return ONLY the JSON array. If no candidates survive validation, return []."""

    return system, f"PUBLICATION:\n{text}"


def build_critique_prompt(candidates: list, text: str) -> tuple[str, str]:
    system = """You are a senior plant ontology curator performing final quality control on a set of candidate ontology terms. Be strict — each term must be well-defined, consistently used in the paper, and genuinely absent from existing ontologies (as confirmed by the ols_search_result field).

Remove candidates that:
- Are too generic or vague
- Are already covered by existing terms (check ols_search_result)
- Appear only once in the paper as a passing mention
- Have a weak or circular definition draft
- Are better expressed as synonyms of existing terms

For each term you KEEP, you may improve the definition_draft if it is imprecise. Return only the kept candidates as a JSON array. If all should be removed, return []."""

    user = f"PUBLICATION (excerpt for context):\n{text[:800]}\n\nCANDIDATES TO REVIEW:\n{json.dumps(candidates, indent=2)}"
    return system, user

# ---------------------------------------------------------------------------
# JSON extraction (same bracket-walking approach as ontology_miner.py)
# ---------------------------------------------------------------------------

def extract_json(raw: str) -> list | None:
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
    print(f"Validated candidates: {len(candidates)}  (high: {len(by_conf['high'])}  medium: {len(by_conf['medium'])}  low: {len(by_conf['low'])})")
    print("─" * 60)

    for c in by_conf["high"] + by_conf["medium"] + by_conf["low"]:
        badge = {"high": "●", "medium": "◐", "low": "○"}.get(c.get("confidence"), "○")
        print(f"\n{badge} [{c.get('ontology', '?')}] {c.get('term', '')}")
        if c.get("namespace"):         print(f"  Namespace : {c['namespace']}")
        if c.get("suggested_parent"):  print(f"  Parent    : {c['suggested_parent']}")
        if c.get("definition_draft"):  print(f"  Def       : {c['definition_draft']}")
        if c.get("synonyms"):          print(f"  Synonyms  : {', '.join(c['synonyms'])}")
        if c.get("ols_search_result"): print(f"  OLS check : {c['ols_search_result']}")
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
            print(f"  {pid:<16} {p['label']}  |  default: {p['default_model']}  |  {key_note}")
        return

    if not any([args.doi, args.text, args.file]):
        print("Usage: python ontology_agent.py --doi|--text|--file <input> [--ontology po|to|both] [--provider <name>]", file=sys.stderr)
        sys.exit(1)

    cfg   = validate_provider(args.provider)
    model = args.model or cfg["default_model"]

    print(f"ontology-agent — agentic term miner for PO/TO")
    print(f"Provider: {cfg['label']}  |  Model: {model}\n")

    pub = get_publication(args)
    print(f"Source  : {pub['source']}" + (f" ({pub['doi']})" if pub.get("doi") else ""))
    if pub.get("title"): print(f"Title   : {pub['title']}")
    print(f"Ontology: {args.ontology.upper()}")

    # Phase 1: Mining agent loop with tool use
    print(f"\n[Phase 1] Mining — agent validating candidates against OLS...")
    mine_system, mine_user = build_mining_prompt(pub["text"], args.ontology)
    try:
        mine_raw = run_agent(mine_system, mine_user, args.provider, model)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    mined_candidates  = extract_json(mine_raw)

    if not mined_candidates:
        print("\nNo candidates identified after OLS validation.")
        sys.exit(0)
    print(f"  → {len(mined_candidates)} candidate(s) passed OLS check")

    # Phase 2: Self-critique pass (single call, no tools)
    print(f"\n[Phase 2] Critique — removing weak or duplicative terms...")
    crit_system, crit_user = build_critique_prompt(mined_candidates, pub["text"])
    try:
        crit_raw = call_plain_llm(crit_system, crit_user, args.provider, model)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    parsed       = extract_json(crit_raw)
    if parsed is None:
        print("  ⚠ Critique response could not be parsed — using mined candidates unfiltered")
        final_candidates = mined_candidates
    else:
        final_candidates = parsed
    print(f"  → {len(final_candidates)} candidate(s) survived critique")

    if final_candidates:
        print_summary(final_candidates)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now  = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    raw_slug = (
        pub.get("doi")
        or pub.get("title")
        or (Path(pub["file"]).stem if pub.get("file") else None)
        or (pub.get("text") or "text")[:50]
    )
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
        "candidate_count": len(final_candidates),
        "candidates":      final_candidates,
    }, indent=2))

    print(f"\nSaved → ontologies/candidates/{out_path.name}")


if __name__ == "__main__":
    main()
