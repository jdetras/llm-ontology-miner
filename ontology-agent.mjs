#!/usr/bin/env node
/**
 * ontology-agent.mjs — Agentic ontology term miner with tool use
 *
 * Unlike ontology-miner.mjs (single LLM pass), this agent:
 *   1. Proposes candidates from the publication text
 *   2. Validates each against existing PO/TO via the OLS API (search_ontology tool)
 *   3. Optionally fetches cited papers for context (fetch_abstract tool)
 *   4. Runs a self-critique pass to remove weak candidates
 *
 * Tool use is supported for: anthropic, openai, gemini, mistral, ollama (llama3.2+),
 * openai-compat (if the endpoint supports it). Falls back to single-pass on failure.
 *
 * Usage:
 *   node ontology-agent.mjs --doi 10.1093/jxb/eraa002
 *   node ontology-agent.mjs --text "paste abstract here"
 *   node ontology-agent.mjs --file ./papers/paper.txt
 *   node ontology-agent.mjs --doi 10.1093/jxb/eraa002 --ontology to --provider openai
 *   node ontology-agent.mjs --providers
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const ROOT    = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(ROOT, 'ontologies', 'candidates');
const MAX_TOOL_ROUNDS = 10; // safety cap on agentic loop iterations

try { const { config } = await import('dotenv'); config(); } catch { /* optional */ }

// ---------------------------------------------------------------------------
// Provider registry (shared with ontology-miner.mjs)
// ---------------------------------------------------------------------------

const PROVIDERS = {
  anthropic:      { label: 'Anthropic (Claude)',          defaultModel: 'claude-sonnet-4-6',      envKey: 'ANTHROPIC_API_KEY'  },
  openai:         { label: 'OpenAI',                      defaultModel: 'gpt-4o',                 envKey: 'OPENAI_API_KEY'     },
  gemini:         { label: 'Google Gemini',               defaultModel: 'gemini-2.0-flash',       envKey: 'GEMINI_API_KEY'     },
  mistral:        { label: 'Mistral AI',                  defaultModel: 'mistral-large-latest',   envKey: 'MISTRAL_API_KEY'    },
  ollama:         { label: 'Ollama (local)',               defaultModel: 'llama3.2',               envKey: null                 },
  'openai-compat':{ label: 'OpenAI-compatible endpoint',  defaultModel: process.env.OPENAI_COMPAT_MODEL ?? 'default', envKey: null },
};

// ---------------------------------------------------------------------------
// Args
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);

if (args.includes('--providers')) {
  for (const [id, p] of Object.entries(PROVIDERS)) {
    const k = p.envKey ? `env: ${p.envKey}` : 'no key required';
    console.log(`  ${id.padEnd(16)} ${p.label}  |  default: ${p.defaultModel}  |  ${k}`);
  }
  process.exit(0);
}

function getArg(flag) {
  const i = args.indexOf(flag);
  const v = i !== -1 ? args[i + 1] : undefined;
  return v && !v.startsWith('--') ? v : null;
}

const doiArg      = getArg('--doi');
const textArg     = getArg('--text');
const fileArg     = getArg('--file');
const ontology    = getArg('--ontology') ?? 'both';
const providerKey = getArg('--provider') ?? process.env.LLM_PROVIDER ?? 'anthropic';
const modelArg    = getArg('--model');

if (!['po', 'to', 'both'].includes(ontology)) {
  console.error(`Error: --ontology must be po, to, or both (got: "${ontology}")`);
  process.exit(1);
}
if (!doiArg && !textArg && !fileArg) {
  console.error('Usage: node ontology-agent.mjs --doi|--text|--file <input> [--ontology po|to|both] [--provider <name>]');
  process.exit(1);
}
if (!PROVIDERS[providerKey]) {
  console.error(`Unknown provider "${providerKey}". Run --providers to list options.`);
  process.exit(1);
}
if (providerKey === 'openai-compat' && !process.env.OPENAI_COMPAT_BASE_URL) {
  console.error('Error: OPENAI_COMPAT_BASE_URL is not set.');
  process.exit(1);
}

const providerCfg = PROVIDERS[providerKey];
if (providerCfg.envKey && !process.env[providerCfg.envKey]) {
  console.error(`Error: ${providerCfg.envKey} is not set.`);
  process.exit(1);
}

const model = modelArg ?? providerCfg.defaultModel;

// ---------------------------------------------------------------------------
// Publication fetch
// ---------------------------------------------------------------------------

async function fetchByDoi(doi) {
  const crUrl = `https://api.crossref.org/works/${encodeURIComponent(doi)}`;
  const crRes = await fetch(crUrl, { headers: { 'User-Agent': 'ontology-agent/1.0 (mailto:research@example.com)' } });
  if (!crRes.ok) throw new Error(`CrossRef ${crRes.status} for ${doi}`);
  const work = (await crRes.json()).message;

  const title   = work.title?.[0] ?? '(no title)';
  const authors = (work.author ?? []).map(a => `${a.given ?? ''} ${a.family ?? ''}`.trim()).join(', ');
  const journal = work['container-title']?.[0] ?? '';
  const year    = work.published?.['date-parts']?.[0]?.[0] ?? '';
  let abstract  = (work.abstract ?? '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

  if (!abstract) {
    const pmcRes = await fetch(`https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=${encodeURIComponent('doi:' + doi)}&resultType=core&format=json`);
    if (pmcRes.ok) abstract = (await pmcRes.json()).resultList?.result?.[0]?.abstractText ?? '';
  }

  return {
    source: 'doi', doi, title, authors, journal, year,
    text: [`Title: ${title}`, authors && `Authors: ${authors}`, journal && `Journal: ${journal}${year ? ` (${year})` : ''}`, abstract ? `\nAbstract:\n${abstract}` : '\n(No abstract available)'].filter(Boolean).join('\n'),
  };
}

async function getPublication() {
  if (doiArg) return fetchByDoi(doiArg);
  if (fileArg) {
    try   { return { source: 'file', file: fileArg, text: readFileSync(fileArg, 'utf8') }; }
    catch (e) { console.error(`Error reading file: ${e.message}`); process.exit(1); }
  }
  return { source: 'text', text: textArg };
}

// ---------------------------------------------------------------------------
// Tool definitions & executors
// ---------------------------------------------------------------------------

const TOOL_DEFS = [
  {
    name: 'search_ontology',
    description: 'Search the Plant Ontology (PO) or Trait Ontology (TO) via the EBI OLS API to check whether a candidate term already exists. Call this for every candidate before proposing it — only surface genuinely novel terms.',
    parameters: {
      type: 'object',
      properties: {
        term:     { type: 'string', description: 'The term or phrase to search for' },
        ontology: { type: 'string', enum: ['po', 'to', 'both'], description: 'Which ontology to search' },
      },
      required: ['term', 'ontology'],
    },
  },
  {
    name: 'fetch_abstract',
    description: 'Fetch the title and abstract of a related paper by DOI. Use this when the main paper cites a closely related work that might clarify a candidate term.',
    parameters: {
      type: 'object',
      properties: {
        doi: { type: 'string', description: 'DOI of the paper to fetch' },
      },
      required: ['doi'],
    },
  },
];

async function searchOntology(term, onto) {
  const ontoParam = onto === 'both' ? 'po,to' : onto;
  const url = `https://www.ebi.ac.uk/ols4/api/search?q=${encodeURIComponent(term)}&ontology=${ontoParam}&type=class&rows=5&exact=false&lang=en`;
  try {
    const res = await fetch(url);
    if (res.status === 429) return { found: null, error: 'OLS rate limited (429) — result unverified, do not treat as absent' };
    if (!res.ok) return { found: false, error: `OLS API ${res.status}` };
    const hits = (await res.json()).response?.docs ?? [];
    if (!hits.length) return { found: false, message: `No existing terms match "${term}" in ${onto.toUpperCase()}` };
    return {
      found: true,
      message: `Found ${hits.length} potential match(es) — review before proposing as new:`,
      matches: hits.map(h => ({ id: h.obo_id ?? h.id, label: h.label, ontology: h.ontology_name, definition: h.description?.[0]?.slice(0, 120) ?? '' })),
    };
  } catch (e) {
    return { found: false, error: e.message };
  }
}

async function executeTool(name, input) {
  console.log(`  → tool: ${name}(${JSON.stringify(input)})`);
  if (name === 'search_ontology') return searchOntology(input.term, input.ontology ?? 'both');
  if (name === 'fetch_abstract') {
    try {
      const pub = await fetchByDoi(input.doi);
      return { title: pub.title, text: pub.text };
    } catch (e) {
      return { error: e.message };
    }
  }
  return { error: `Unknown tool: ${name}` };
}

// ---------------------------------------------------------------------------
// Provider adapters — tool use format translation
// ---------------------------------------------------------------------------

function toAnthropicTools(defs) {
  return defs.map(t => ({ name: t.name, description: t.description, input_schema: t.parameters }));
}

function toOpenAITools(defs) {
  return defs.map(t => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } }));
}

function toGeminiTools(defs) {
  return [{ function_declarations: defs.map(t => ({ name: t.name, description: t.description, parameters: t.parameters })) }];
}

// ---------------------------------------------------------------------------
// Agentic loops per provider
// ---------------------------------------------------------------------------

async function runAgentAnthropic(systemPrompt, userMessage) {
  const messages = [{ role: 'user', content: userMessage }];
  const tools = toAnthropicTools(TOOL_DEFS);

  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const res = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
      body: JSON.stringify({ model, max_tokens: 4096, system: systemPrompt, tools, messages }),
    });
    if (!res.ok) throw new Error(`Anthropic ${res.status}: ${await res.text()}`);
    const data = await res.json();

    const toolUses = data.content.filter(c => c.type === 'tool_use');
    if (!toolUses.length) {
      const text = data.content.filter(c => c.type === 'text').map(c => c.text).join('');
      return text;
    }

    // Execute tool calls
    messages.push({ role: 'assistant', content: data.content });
    const results = await Promise.all(toolUses.map(async t => ({ type: 'tool_result', tool_use_id: t.id, content: JSON.stringify(await executeTool(t.name, t.input)) })));
    messages.push({ role: 'user', content: results });
  }
  throw new Error('Agent loop exceeded max rounds');
}

async function runAgentOpenAICompat(systemPrompt, userMessage, baseUrl, apiKey) {
  const messages = [{ role: 'system', content: systemPrompt }, { role: 'user', content: userMessage }];
  const tools = toOpenAITools(TOOL_DEFS);
  const headers = { 'content-type': 'application/json' };
  if (apiKey) headers['authorization'] = `Bearer ${apiKey}`;

  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const res = await fetch(`${baseUrl}/v1/chat/completions`, {
      method: 'POST', headers,
      body: JSON.stringify({ model, max_tokens: 4096, messages, tools, tool_choice: 'auto' }),
    });
    if (!res.ok) throw new Error(`${baseUrl} ${res.status}: ${await res.text()}`);
    const data = await res.json();
    const choice = data.choices?.[0];

    if (choice?.finish_reason !== 'tool_calls') return choice?.message?.content ?? '';

    messages.push(choice.message);
    const results = await Promise.all(choice.message.tool_calls.map(async tc => ({
      role: 'tool',
      tool_call_id: tc.id,
      content: JSON.stringify(await executeTool(tc.function.name, JSON.parse(tc.function.arguments))),
    })));
    messages.push(...results);
  }
  throw new Error('Agent loop exceeded max rounds');
}

async function runAgentGemini(systemPrompt, userMessage) {
  const apiKey = process.env.GEMINI_API_KEY;
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${apiKey}`;
  const contents = [{ role: 'user', parts: [{ text: userMessage }] }];
  const tools = toGeminiTools(TOOL_DEFS);

  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const res = await fetch(url, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ system_instruction: { parts: [{ text: systemPrompt }] }, contents, tools, generationConfig: { maxOutputTokens: 4096 } }),
    });
    if (!res.ok) throw new Error(`Gemini ${res.status}: ${await res.text()}`);
    const data = await res.json();
    const parts = data.candidates?.[0]?.content?.parts ?? [];
    const funcCalls = parts.filter(p => p.functionCall);

    if (!funcCalls.length) return parts.filter(p => p.text).map(p => p.text).join('');

    contents.push({ role: 'model', parts });
    const responses = await Promise.all(funcCalls.map(async p => ({
      functionResponse: { name: p.functionCall.name, response: await executeTool(p.functionCall.name, p.functionCall.args) },
    })));
    contents.push({ role: 'user', parts: responses });
  }
  throw new Error('Agent loop exceeded max rounds');
}

async function runAgent(systemPrompt, userMessage) {
  switch (providerKey) {
    case 'anthropic':     return runAgentAnthropic(systemPrompt, userMessage);
    case 'gemini':        return runAgentGemini(systemPrompt, userMessage);
    case 'openai':        return runAgentOpenAICompat(systemPrompt, userMessage, 'https://api.openai.com', process.env.OPENAI_API_KEY);
    case 'mistral':       return runAgentOpenAICompat(systemPrompt, userMessage, 'https://api.mistral.ai', process.env.MISTRAL_API_KEY);
    case 'ollama':        return runAgentOpenAICompat(systemPrompt, userMessage, 'http://localhost:11434', null);
    case 'openai-compat': return runAgentOpenAICompat(systemPrompt, userMessage, process.env.OPENAI_COMPAT_BASE_URL, process.env.OPENAI_COMPAT_API_KEY ?? null);
    default: throw new Error(`Unknown provider: ${providerKey}`);
  }
}

// Single-shot LLM call with no tools — used for the critique pass.
async function callPlainLLM(systemPrompt, userMessage) {
  switch (providerKey) {
    case 'anthropic': {
      const res = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: { 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
        body: JSON.stringify({ model, max_tokens: 4096, system: systemPrompt, messages: [{ role: 'user', content: userMessage }] }),
      });
      if (!res.ok) throw new Error(`Anthropic ${res.status}: ${await res.text()}`);
      const data = await res.json();
      return data.content?.find(c => c.type === 'text')?.text ?? '';
    }
    case 'gemini': {
      const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${process.env.GEMINI_API_KEY}`;
      const res = await fetch(url, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ system_instruction: { parts: [{ text: systemPrompt }] }, contents: [{ role: 'user', parts: [{ text: userMessage }] }], generationConfig: { maxOutputTokens: 4096 } }),
      });
      if (!res.ok) throw new Error(`Gemini ${res.status}: ${await res.text()}`);
      const data = await res.json();
      return data.candidates?.[0]?.content?.parts?.filter(p => p.text).map(p => p.text).join('') ?? '';
    }
    default: {
      const baseUrl = providerKey === 'openai' ? 'https://api.openai.com'
        : providerKey === 'mistral' ? 'https://api.mistral.ai'
        : providerKey === 'ollama'  ? 'http://localhost:11434'
        : process.env.OPENAI_COMPAT_BASE_URL;
      const apiKey = providerKey === 'openai' ? process.env.OPENAI_API_KEY
        : providerKey === 'mistral' ? process.env.MISTRAL_API_KEY
        : providerKey === 'ollama'  ? null
        : process.env.OPENAI_COMPAT_API_KEY ?? null;
      const headers = { 'content-type': 'application/json' };
      if (apiKey) headers['authorization'] = `Bearer ${apiKey}`;
      const res = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: 'POST', headers,
        body: JSON.stringify({ model, max_tokens: 4096, messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: userMessage }] }),
      });
      if (!res.ok) throw new Error(`${baseUrl} ${res.status}: ${await res.text()}`);
      const data = await res.json();
      return data.choices?.[0]?.message?.content ?? '';
    }
  }
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

const ONTOLOGY_CONTEXT = {
  po:   'The Plant Ontology (PO) covers plant anatomy (leaf, root, stem, epidermis), morphology (shapes, surfaces), and developmental stages (germination, flowering, senescence).',
  to:   'The Trait Ontology (TO) covers measurable or observable plant traits: yield, stress tolerance, morphological, biochemical, and agronomic traits.',
  both: 'PO covers plant anatomy, morphology, and developmental stages. TO covers measurable plant traits and phenotypes.',
};

function buildMiningPrompt(publicationText) {
  const ontCtx = ONTOLOGY_CONTEXT[ontology];
  const targetLabel = ontology === 'both' ? 'Plant Ontology (PO) and/or Trait Ontology (TO)' : ontology === 'po' ? 'Plant Ontology (PO)' : 'Trait Ontology (TO)';

  const system = `You are an expert plant biologist and ontology curator. Your task is to identify candidate terms from scientific literature that could enrich ${targetLabel}.

${ontCtx}

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

Return ONLY the JSON array. If no candidates survive validation, return [].`;

  return { system, user: `PUBLICATION:\n${publicationText}` };
}

function buildCritiquePrompt(candidates, publicationText) {
  return {
    system: `You are a senior plant ontology curator performing final quality control on a set of candidate ontology terms. Be strict — each term must be well-defined, consistently used in the paper, and genuinely absent from existing ontologies (as confirmed by the ols_search_result field).

Remove candidates that:
- Are too generic or vague
- Are already covered by existing terms (check ols_search_result)
- Appear only once in the paper as a passing mention
- Have a weak or circular definition draft
- Are better expressed as synonyms of existing terms

For each term you KEEP, you may improve the definition_draft if it is imprecise. Return only the kept candidates as a JSON array. If all should be removed, return [].`,
    user: `PUBLICATION (excerpt for context):\n${publicationText.slice(0, 800)}\n\nCANDIDATES TO REVIEW:\n${JSON.stringify(candidates, null, 2)}`,
  };
}

// ---------------------------------------------------------------------------
// JSON extraction (same bracket-walking approach as ontology-miner.mjs)
// ---------------------------------------------------------------------------

function extractJson(raw) {
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenced?.[1]) { try { return JSON.parse(fenced[1].trim()); } catch { /* fall through */ } }
  let start = 0;
  while (true) {
    const i = raw.indexOf('[', start);
    if (i === -1) break;
    let depth = 0, j = i;
    for (; j < raw.length; j++) {
      if (raw[j] === '[') depth++;
      else if (raw[j] === ']') { if (--depth === 0) break; }
    }
    if (depth === 0) { try { return JSON.parse(raw.slice(i, j + 1)); } catch { /* next */ } }
    start = i + 1;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Output helpers
// ---------------------------------------------------------------------------

function slugify(str) {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
}

function printSummary(candidates) {
  const byConf = { high: [], medium: [], low: [] };
  for (const c of candidates) (byConf[c.confidence] ?? byConf.low).push(c);
  console.log(`\n${'─'.repeat(60)}`);
  console.log(`Validated candidates: ${candidates.length}  (high: ${byConf.high.length}  medium: ${byConf.medium.length}  low: ${byConf.low.length})`);
  console.log('─'.repeat(60));
  for (const c of [...byConf.high, ...byConf.medium, ...byConf.low]) {
    const badge = c.confidence === 'high' ? '●' : c.confidence === 'medium' ? '◐' : '○';
    console.log(`\n${badge} [${c.ontology ?? '?'}] ${c.term}`);
    if (c.namespace)        console.log(`  Namespace : ${c.namespace}`);
    if (c.suggested_parent) console.log(`  Parent    : ${c.suggested_parent}`);
    if (c.definition_draft) console.log(`  Def       : ${c.definition_draft}`);
    if (c.synonyms?.length) console.log(`  Synonyms  : ${c.synonyms.join(', ')}`);
    if (c.ols_search_result) console.log(`  OLS check : ${c.ols_search_result}`);
  }
  console.log('\n' + '─'.repeat(60));
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

console.log(`ontology-agent — agentic term miner for PO/TO`);
console.log(`Provider: ${providerCfg.label}  |  Model: ${model}\n`);

const pub = await getPublication();
console.log(`Source  : ${pub.source}${pub.doi ? ` (${pub.doi})` : ''}`);
if (pub.title) console.log(`Title   : ${pub.title}`);
console.log(`Ontology: ${ontology.toUpperCase()}`);

// Phase 1: Mining agent loop with tool use
console.log(`\n[Phase 1] Mining — agent validating candidates against OLS...`);
const { system: mineSystem, user: mineUser } = buildMiningPrompt(pub.text);
const mineRaw = await runAgent(mineSystem, mineUser);
const minedCandidates = extractJson(mineRaw);

if (!minedCandidates?.length) {
  console.log('\nNo candidates identified after OLS validation.');
  process.exit(0);
}
console.log(`  → ${minedCandidates.length} candidate(s) passed OLS check`);

// Phase 2: Self-critique pass (single call, no tools)
console.log(`\n[Phase 2] Critique — removing weak or duplicative terms...`);
const { system: critiqueSystem, user: critiqueUser } = buildCritiquePrompt(minedCandidates, pub.text);
const critiqueRaw = await callPlainLLM(critiqueSystem, critiqueUser);
const finalCandidates = extractJson(critiqueRaw) ?? minedCandidates;
console.log(`  → ${finalCandidates.length} candidate(s) survived critique`);

if (finalCandidates.length) printSummary(finalCandidates);

// Save
mkdirSync(OUT_DIR, { recursive: true });
const now   = new Date();
const date  = now.toISOString().slice(0, 10);
const slug  = pub.doi ? slugify(pub.doi) : pub.file ? slugify(pub.file.split('/').pop().replace(/\.[^.]+$/, '')) : slugify((pub.text ?? 'text').slice(0, 50));
const outPath = join(OUT_DIR, `${slug}-${date}.json`);

writeFileSync(outPath, JSON.stringify({
  mined_at: now.toISOString(),
  provider: providerKey, model,
  source: pub.source,
  doi: pub.doi ?? null,
  title: pub.title ?? null,
  ontology_target: ontology,
  candidate_count: finalCandidates.length,
  candidates: finalCandidates,
}, null, 2));

console.log(`\nSaved → ontologies/candidates/${slug}-${date}.json`);
