# llm-ontology-miner

Extract candidate ontology terms from plant science journal publications using any LLM. Surfaces terms that could enrich the [Plant Ontology (PO)](http://plantontology.org) and [Trait Ontology (TO)](https://www.cropontology.org) from literature — with draft definitions, suggested parent terms, synonyms, and curator rationale.

## Tools

| Script | Mode | Description |
|---|---|---|
| `ontology_miner.py` | Single-pass | Fast single LLM call — good for quick exploration |
| `ontology_agent.py` | Agentic | Multi-step agent: validates candidates against OLS, follows citations, self-critiques |
| `journal_watcher.py` | Scheduled | Monitors a journal watchlist for new papers and feeds them to the agent automatically |
| `export_candidates.py` | Export | Converts candidate JSON to submission-ready formats for ontology curators |

## How it works

### Single-pass (`ontology-miner.mjs`)
1. Provide a publication via DOI or pasted text
2. The tool fetches the abstract (CrossRef → Europe PMC fallback) or uses your text directly
3. An LLM analyzes the text and returns structured candidate terms
4. Results are printed and saved as JSON

### Agentic (`ontology-agent.mjs`)
1. Provide a publication via DOI or pasted text
2. **Phase 1 — Mining agent**: The LLM proposes candidates, then calls the [EBI OLS API](https://www.ebi.ac.uk/ols) to check each one against existing PO/TO terms before surfacing it. Can follow cited DOIs for additional context.
3. **Phase 2 — Critique agent**: A second LLM pass removes weak, generic, or already-covered candidates
4. Results are printed and saved as JSON

### Scheduled (`journal-watcher.mjs`)
1. Define a list of journals to monitor in `journals.yml`
2. The watcher polls CrossRef for papers published since the last run
3. Each new paper is fed to the agent automatically
4. History is tracked to avoid reprocessing

## Installation

```bash
git clone https://github.com/jdetras/llm-ontology-miner.git
cd llm-ontology-miner
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # add your API key(s)
```

Python 3.10+ required. After the first setup, activate the venv (`source .venv/bin/activate`) before running any commands.

## Usage

### Single-pass miner

```bash
python ontology_miner.py --doi 10.1093/jxb/eraa002
python ontology_miner.py --text "Paste abstract or full text here"
python ontology_miner.py --file ./papers/my-paper.txt
python ontology_miner.py --doi 10.xxxx/xxx --ontology po   # po | to | both
python ontology_miner.py --doi 10.xxxx/xxx --provider openai
python ontology_miner.py --providers
```

### Agentic miner (recommended)

Same flags as the single-pass miner:

```bash
python ontology_agent.py --doi 10.1093/jxb/eraa002
python ontology_agent.py --doi 10.xxxx/xxx --ontology to --provider openai
python ontology_agent.py --text "abstract text" --provider ollama --model llama3.2
```

The agent will print tool call activity as it runs:
```
[Phase 1] Mining — agent validating candidates against OLS...
  → tool: search_ontology({"term":"leaf rolling","ontology":"to"})
  → tool: search_ontology({"term":"coleoptile elongation zone","ontology":"po"})
  → 4 candidate(s) passed OLS check
[Phase 2] Critique — removing weak or duplicative terms...
  → 3 candidate(s) survived critique
```

### Scheduled journal watcher

```bash
# 1. Set up your journal watchlist
cp journals.example.yml journals.yml
# edit journals.yml to add/remove journals and set your preferred provider

# 2. Test with a dry run (no LLM calls)
python journal_watcher.py --dry-run

# 3. Run now
python journal_watcher.py

# 4. Check what has been processed
python journal_watcher.py --status

# 5. Set up automated scheduling
python journal_watcher.py --cron   # prints crontab lines to copy
```

## Supported LLM providers

| Flag | Provider | Default model | Key required |
|---|---|---|---|
| `--provider anthropic` | Anthropic (Claude) | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `--provider openai` | OpenAI | `gpt-4o` | `OPENAI_API_KEY` |
| `--provider gemini` | Google Gemini | `gemini-2.0-flash` | `GEMINI_API_KEY` |
| `--provider mistral` | Mistral AI | `mistral-large-latest` | `MISTRAL_API_KEY` |
| `--provider ollama` | Ollama (local) | `llama3.2` | none |
| `--provider openai-compat` | Any OpenAI-compatible endpoint | `OPENAI_COMPAT_MODEL` | `OPENAI_COMPAT_BASE_URL` |

Override the model for any provider with `--model`:

```bash
python ontology_miner.py --doi 10.xxxx/xxx --provider anthropic --model claude-opus-4-8
python ontology_miner.py --text "..." --provider ollama --model gemma3:12b
```

Set a default provider via env var to skip the flag each time:

```bash
LLM_PROVIDER=openai python ontology_miner.py --doi 10.xxxx/xxx
```

## Output

Each candidate term includes:

```json
{
  "term": "coleoptile elongation zone",
  "ontology": "PO",
  "namespace": "plant_anatomy",
  "definition_draft": "A region of the coleoptile characterized by rapid cell elongation during early seedling development.",
  "suggested_parent": "coleoptile",
  "synonyms": ["elongation region", "CEZ"],
  "source_sentence": "Cell division ceased in the meristematic zone while elongation continued distally in the coleoptile elongation zone.",
  "rationale": "The term is used consistently throughout the paper to describe a spatially distinct region not currently represented as a separate PO term.",
  "confidence": "high"
}
```

Results are saved to `ontologies/candidates/{slug}-{date}.json` and include the provider and model used.

## Curator role — human in the loop

This tool is a **literature triage assistant**, not a replacement for expert curation. The LLM surfaces candidates; a human curator makes all final decisions.

### What the tool does automatically

- Fetches abstracts from CrossRef and Europe PMC
- Searches EBI OLS to flag terms that already exist in PO/TO
- Runs a self-critique pass to remove generic or weakly-evidenced candidates
- Assigns a preliminary confidence score (`high / medium / low`)
- Drafts definitions and suggests parent terms from the paper's language

### What curators must do

| Step | Curator responsibility |
|---|---|
| **Review** | Read every candidate against the source paper — the LLM can hallucinate context |
| **Validate OLS** | Confirm the OLS check result; search manually for near-synonyms the agent may have missed |
| **Refine definitions** | Rewrite `definition_draft` in standard ontology style: a genus-differentia structure, free of hedges and first-person language |
| **Resolve parent** | Replace `suggested_parent` with a verified OBO accession (e.g. `PO:0020144`) — never commit a term with a plain-text parent |
| **Replace IDs** | Assign real accessions (replacing `PO:NEWTERM_001` placeholders), following the ontology's ID minting policy |
| **Cross-reference** | Add `xref:` lines to related terms, synonyms from other ontologies, and the source DOI |
| **Submit** | Open a GitHub NTR issue or PR in the target ontology's repo; tag the relevant editors |

### Confidence scores

The LLM confidence score is a **triage signal**, not a quality guarantee:

- `high` — term appears multiple times with consistent usage, clear definition, no OLS match found
- `medium` — plausible candidate but limited evidence or some OLS overlap; needs more scrutiny
- `low` — borderline: single occurrence, vague usage, or partial OLS match; consider discarding

Curators should always read the `source_sentence` and `ols_search_result` fields, regardless of score.

### Recommended workflow

```
1. journal-watcher scans for new papers         (automated)
2. ontology-agent mines and validates candidates (automated)
3. Curator reviews CSV or NTR markdown          (human)
4. Curator refines definitions and resolves IDs  (human)
5. Curator submits GitHub NTR issue or PR       (human)
6. Ontology editors review and merge            (human)
```

The tool handles steps 1–2. Steps 3–6 require domain expertise that no LLM can substitute for.

## Exporting for submission

After mining, convert candidates to curator-ready formats:

```bash
# List all candidate files
python export_candidates.py --list

# Export all formats (default)
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json

# Export a specific format
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json --format obo
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json --format github
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json --format robot
python export_candidates.py --input ontologies/candidates/my-paper-2026-06-05.json --format csv
```

Outputs are saved to `ontologies/exports/`.

### Output formats

| Format | File | Use |
|---|---|---|
| **OBO stanzas** | `.obo` | Paste into a `.obo` file or include in a GitHub PR to the ontology repo |
| **ROBOT template** | `-robot.tsv` | Import with `robot template` — standard batch submission for OBO Foundry ontologies |
| **GitHub NTR** | `-ntr.md` | Copy each term block and open a New Term Request issue in the ontology's GitHub repo |
| **CSV** | `.csv` | Human review in a spreadsheet before submission |

### Where to submit

| Ontology | GitHub Issues |
|---|---|
| PO — Plant Ontology | https://github.com/Planteome/plant-ontology/issues/new |
| TO — Trait Ontology | https://github.com/Planteome/plant-trait-ontology/issues/new |
| PECO | https://github.com/Planteome/plant-experimental-conditions-ontology/issues/new |
| FLOPO | https://github.com/flora-phenotype-ontology/flopoontology/issues/new |

> **Note:** Placeholder IDs (`PO:NEWTERM_001`) must be replaced with real accessions assigned by ontology curators. Always review `definition_draft` and `suggested_parent` before submitting.

## Ontology targets

The `--ontology` flag currently accepts `po`, `to`, or `both`. The reference landscape below covers the full set of accepted plant and agricultural ontologies — useful for knowing which namespace a candidate term belongs to, or for extending the tool to target additional ontologies.

### Core plant ontologies — [OBO Foundry](https://obofoundry.org) / [Planteome](https://planteome.org)

| ID | Name | Covers |
|---|---|---|
| **PO** | [Plant Ontology](http://plantontology.org) | Anatomy, morphology, developmental stages |
| **TO** | [Trait Ontology](https://www.cropontology.org) | Plant traits and phenotypes |
| **PECO** | [Plant Experimental Conditions Ontology](https://www.ebi.ac.uk/ols/ontologies/peco) | Growth conditions, treatments, stresses |
| **PPO** | [Plant Phenology Ontology](https://www.plantphenology.org) | Phenological stages (leafing, flowering timing) |
| **GO** | [Gene Ontology](https://geneontology.org) | Biological process, molecular function, cellular component |

### Crop-specific — [Crop Ontology](https://cropontology.org) / CGIAR

| ID | Name | Covers |
|---|---|---|
| **CO_321** | Wheat Crop Ontology | Wheat-specific variables and traits |
| **CO_322** | Maize Crop Ontology | Maize-specific variables and traits |
| **CO_340** | Rice Crop Ontology | Rice-specific variables and traits |
| **CO_356** | Potato Crop Ontology | Potato-specific variables and traits |
| **GRO** | [Gramene Crop Ontology](https://www.gramene.org) | Grass and cereal crops |

### Phenotype and morphology

| ID | Name | Covers |
|---|---|---|
| **FLOPO** | [Flora Phenotype Ontology](https://www.ebi.ac.uk/ols/ontologies/flopo) | Plant morphological phenotypes |
| **OBA** | [Ontology of Biological Attributes](https://www.ebi.ac.uk/ols/ontologies/oba) | Measurable biological attributes |

### Supporting ontologies (commonly cross-referenced)

| ID | Name | Covers |
|---|---|---|
| **CHEBI** | [Chemical Entities of Biological Interest](https://www.ebi.ac.uk/chebi) | Metabolites, compounds, biochemicals |
| **ENVO** | [Environment Ontology](https://sites.google.com/site/environmentontology) | Habitats, abiotic conditions, biomes |
| **NCBITaxon** | [NCBI Taxonomy](https://www.ncbi.nlm.nih.gov/taxonomy) | Species and taxon identifiers |
| **RO** | [Relations Ontology](https://www.obofoundry.org/ontology/ro.html) | is_a, part_of, regulates, and other relations |
| **AGROVOC** | [FAO Agricultural Thesaurus](https://agrovoc.fao.org) | Broad agricultural vocabulary (SKOS, not strict OWL) |

### Registries to browse

- **[OBO Foundry](https://obofoundry.org)** — curated, peer-reviewed biological ontologies
- **[Planteome](https://planteome.org)** — plant-specific ontology collection
- **[AgroPortal](https://agroportal.lirmm.fr)** — agricultural and food ontologies
- **[Crop Ontology](https://cropontology.org)** — CGIAR crop trait variables
- **[OLS (EBI)](https://www.ebi.ac.uk/ols)** — general ontology search across all life sciences

## License

MIT
