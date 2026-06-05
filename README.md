# llm-ontology-miner

Extract candidate ontology terms from plant science journal publications using any LLM. Surfaces terms that could enrich the [Plant Ontology (PO)](http://plantontology.org) and [Trait Ontology (TO)](https://www.cropontology.org) from literature — with draft definitions, suggested parent terms, synonyms, and curator rationale.

## How it works

1. Provide a publication via DOI or pasted text
2. The tool fetches the abstract (CrossRef → Europe PMC fallback) or uses your text directly
3. An LLM analyzes the text as an ontology curator and returns structured candidate terms
4. Results are printed to the console and saved as JSON for downstream review

## Installation

```bash
git clone https://github.com/jdetras/llm-ontology-miner.git
cd llm-ontology-miner
npm install
cp .env.example .env   # add your API key(s)
```

Node.js 18+ required.

## Usage

```bash
# From a DOI
node ontology-miner.mjs --doi 10.1093/jxb/eraa002

# From pasted text
node ontology-miner.mjs --text "Paste abstract or full text here"

# From a local file
node ontology-miner.mjs --file ./papers/my-paper.txt

# Target a specific ontology (default: both)
node ontology-miner.mjs --doi 10.xxxx/xxx --ontology po
node ontology-miner.mjs --doi 10.xxxx/xxx --ontology to

# Choose a provider
node ontology-miner.mjs --doi 10.xxxx/xxx --provider openai
node ontology-miner.mjs --doi 10.xxxx/xxx --provider ollama --model llama3.2

# List available providers
node ontology-miner.mjs --providers
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

- **PO — Plant Ontology**: anatomy (leaf, root, epidermis), morphology (shapes, surfaces), developmental stages (germination, flowering, senescence)
- **TO — Trait Ontology**: yield traits, stress tolerance, morphological, biochemical, and agronomic traits

## License

MIT
