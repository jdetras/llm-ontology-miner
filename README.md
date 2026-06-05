# llm-ontology-miner

Extract candidate ontology terms from plant science journal publications using any LLM. Surfaces terms that could enrich the [Plant Ontology (PO)](http://plantontology.org) and [Trait Ontology (TO)](https://www.cropontology.org) from literature — with draft definitions, suggested parent terms, synonyms, and curator rationale.

## Tools

| Script | Mode | Description |
|---|---|---|
| `ontology-miner.mjs` | Single-pass | Fast single LLM call — good for quick exploration |
| `ontology-agent.mjs` | Agentic | Multi-step agent: validates candidates against OLS, follows citations, self-critiques |
| `journal-watcher.mjs` | Scheduled | Monitors a journal watchlist for new papers and feeds them to the agent automatically |

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
npm install
cp .env.example .env   # add your API key(s)
```

Node.js 18+ required.

## Usage

### Single-pass miner

```bash
node ontology-miner.mjs --doi 10.1093/jxb/eraa002
node ontology-miner.mjs --text "Paste abstract or full text here"
node ontology-miner.mjs --file ./papers/my-paper.txt
node ontology-miner.mjs --doi 10.xxxx/xxx --ontology po   # po | to | both
node ontology-miner.mjs --doi 10.xxxx/xxx --provider openai
node ontology-miner.mjs --providers
```

### Agentic miner (recommended)

Same flags as the single-pass miner:

```bash
node ontology-agent.mjs --doi 10.1093/jxb/eraa002
node ontology-agent.mjs --doi 10.xxxx/xxx --ontology to --provider openai
node ontology-agent.mjs --text "abstract text" --provider ollama --model llama3.1
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
node journal-watcher.mjs --dry-run

# 3. Run now
node journal-watcher.mjs

# 4. Check what has been processed
node journal-watcher.mjs --status

# 5. Set up automated scheduling
node journal-watcher.mjs --cron   # prints crontab line to copy
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
node ontology-miner.mjs --doi 10.xxxx/xxx --provider anthropic --model claude-opus-4-8
node ontology-miner.mjs --text "..." --provider ollama --model gemma3:12b
```

Set a default provider via env var to skip the flag each time:

```bash
LLM_PROVIDER=openai node ontology-miner.mjs --doi 10.xxxx/xxx
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
