# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests (87 tests, no network calls)
python -m pytest test_all.py -v

# Run a single test class
python -m pytest test_all.py::TestValidateProvider -v

# List available LLM providers
python ontology_miner.py --providers

# Single-pass miner
python ontology_miner.py --doi 10.1093/jxb/eraa002
python ontology_miner.py --text "abstract text" --provider openai --ontology to

# Agentic miner (recommended — validates against OLS, runs self-critique)
python ontology_agent.py --doi 10.1093/jxb/eraa002
python ontology_agent.py --text "abstract text" --provider ollama --model llama3.2

# Journal watcher
python journal_watcher.py --dry-run   # preview without LLM calls
python journal_watcher.py --status    # show history
python journal_watcher.py --cron      # print crontab lines

# Export to submission formats
python export_candidates.py --list
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json --format obo
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in API keys
cp journals.example.yml journals.yml   # for journal_watcher only
```

The `LLM_PROVIDER` env var sets the default provider so `--provider` can be omitted.

## Architecture

Four entry-point scripts with a shared `providers.py` module:

```
providers.py          — LLM provider registry, call_plain_llm(), run_agent()
ontology_miner.py     — Single-pass: one LLM call, returns JSON array of candidates
ontology_agent.py     — Agentic: Phase 1 (tool-use loop → OLS validation) + Phase 2 (critique call)
journal_watcher.py    — Cron-style scanner: polls CrossRef, calls ontology_agent.py per paper
export_candidates.py  — Converts candidates JSON → OBO / ROBOT TSV / GitHub NTR markdown / CSV
```

### providers.py — single source of truth for LLM calls

`PROVIDERS` dict maps provider keys to labels, default models, and env var names. `call_plain_llm()` is a single-shot call with no tools; `run_agent()` dispatches to provider-specific agentic loops (`_run_agent_anthropic`, `_run_agent_openai_compat`, `_run_agent_gemini`). Ollama uses its native `/api/chat` endpoint instead of the OpenAI-compat path to handle thinking-model quirks (Qwen3, etc.).

### ontology_agent.py — two-phase pipeline

- **Phase 1 (mining loop):** Up to `MAX_TOOL_ROUNDS=10` tool-use rounds. The agent calls `search_ontology` (EBI OLS4 API) for each candidate and optionally `fetch_abstract` (CrossRef) for cited DOIs. Terminates when no tool calls are returned.
- **Phase 2 (critique):** Single `call_plain_llm` call to remove weak, generic, or duplicative candidates. If JSON parsing fails, falls back to unfiltered Phase 1 output.

### Tool definitions

`TOOL_DEFS` in `ontology_agent.py` uses a provider-neutral schema. Three converters (`_to_anthropic_tools`, `_to_openai_tools`, `_to_gemini_tools`) translate to provider-specific formats. Gemini requires function responses under `role: 'user'` (not `'function'`).

### Data flow

```
Input: --doi / --text / --file
  ↓ fetch_by_doi (CrossRef → Europe PMC fallback) or read directly
  ↓ build_mining_prompt / build_prompts
  ↓ LLM call(s) with optional tool use
  ↓ extract_json (handles ```json blocks and bare arrays)
  ↓ ontologies/candidates/{slug}-{date}.json
  ↓ export_candidates.py → ontologies/exports/
```

### File outputs

- `ontologies/candidates/{slug}-{date}.json` — raw candidate list with metadata (provider, model, DOI, mined_at)
- `ontologies/exports/{slug}.obo` — OBO stanzas with placeholder IDs (`PO:NEWTERM_001`)
- `ontologies/exports/{slug}-robot.tsv` — ROBOT template for batch OBO Foundry import
- `ontologies/exports/{slug}-ntr.md` — GitHub New Term Request markdown
- `ontologies/exports/{slug}.csv` — CSV for spreadsheet review
- `ontologies/scan-history.json` — DOIs processed/failed by journal_watcher

### Adding a new provider

1. Add an entry to `PROVIDERS` and `_PROVIDER_BASES` in `providers.py`.
2. If it uses OpenAI-compat `/v1/chat/completions`, no new agent loop is needed — `_run_agent_openai_compat` handles it.
3. If it needs a custom protocol (like Anthropic or Gemini), add a `_run_agent_<name>` function and wire it into `run_agent()`.
