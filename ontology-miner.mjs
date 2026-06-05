#!/usr/bin/env node
/**
 * ontology-miner.mjs — Extract candidate ontology terms from plant/trait literature
 *
 * Reads a journal publication (via DOI or pasted text) and uses an LLM to surface
 * terms that could be added to the Plant Ontology (PO) or Trait Ontology (TO).
 *
 * Usage:
 *   node ontology-miner.mjs --doi 10.1093/jxb/eraa002
 *   node ontology-miner.mjs --text "paste abstract or full text here"
 *   node ontology-miner.mjs --file ./papers/my-paper.txt
 *   node ontology-miner.mjs --doi 10.1093/jxb/eraa002 --ontology to
 *   node ontology-miner.mjs --doi 10.1093/jxb/eraa002 --provider openai
 *   node ontology-miner.mjs --doi 10.1093/jxb/eraa002 --provider ollama --model llama3.2
 *   node ontology-miner.mjs --providers   (list available providers)
 *
 * Providers:  anthropic | openai | gemini | mistral | ollama | openai-compat
 * Output:     JSON saved to ontologies/candidates/{slug}-{date}.json
 *
 * Required env vars per provider:
 *   anthropic     ANTHROPIC_API_KEY
 *   openai        OPENAI_API_KEY
 *   gemini        GEMINI_API_KEY
 *   mistral       MISTRAL_API_KEY
 *   ollama        (none — local server at http://localhost:11434)
 *   openai-compat OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY (optional), OPENAI_COMPAT_MODEL (optional)
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const ROOT = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(ROOT, 'ontologies', 'candidates');

// --- load .env if present ---
try {
  const { config } = await import('dotenv');
  config();
} catch { /* dotenv optional */ }

// ---------------------------------------------------------------------------
// Provider registry
// ---------------------------------------------------------------------------

const PROVIDERS = {
  anthropic: {
    label: 'Anthropic (Claude)',
    defaultModel: 'claude-sonnet-4-6',
    envKey: 'ANTHROPIC_API_KEY',
  },
  openai: {
    label: 'OpenAI',
    defaultModel: 'gpt-4o',
    envKey: 'OPENAI_API_KEY',
  },
  gemini: {
    label: 'Google Gemini',
    defaultModel: 'gemini-2.0-flash',
    envKey: 'GEMINI_API_KEY',
  },
  mistral: {
    label: 'Mistral AI',
    defaultModel: 'mistral-large-latest',
    envKey: 'MISTRAL_API_KEY',
  },
  ollama: {
    label: 'Ollama (local)',
    defaultModel: 'llama3.2',
    envKey: null,
  },
  'openai-compat': {
    label: 'OpenAI-compatible endpoint',
    defaultModel: process.env.OPENAI_COMPAT_MODEL ?? 'default',
    envKey: null, // key is optional — keyless endpoints (vLLM, LM Studio) are supported
  },
};

// ---------------------------------------------------------------------------
// Arg parsing
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);

if (args.includes('--providers')) {
  console.log('Available providers:\n');
  for (const [id, p] of Object.entries(PROVIDERS)) {
    const keyNote = p.envKey ? `env: ${p.envKey}` : 'no key required';
    console.log(`  ${id.padEnd(16)} ${p.label}`);
    console.log(`  ${''.padEnd(16)} default model: ${p.defaultModel}  |  ${keyNote}`);
    console.log();
  }
  process.exit(0);
}

function getArg(flag) {
  const i = args.indexOf(flag);
  const value = i !== -1 ? args[i + 1] : undefined;
  return value && !value.startsWith('--') ? value : null;
}

const doiArg      = getArg('--doi');
const textArg     = getArg('--text');
const fileArg     = getArg('--file');
const ontology    = getArg('--ontology') ?? 'both';
if (!['po', 'to', 'both'].includes(ontology)) {
  console.error(`Error: --ontology must be one of: po, to, both (got: "${ontology}")`);
  process.exit(1);
}
const providerKey = getArg('--provider') ?? process.env.LLM_PROVIDER ?? 'anthropic';
const modelArg    = getArg('--model');

if (!doiArg && !textArg && !fileArg) {
  console.error('Usage:');
  console.error('  node ontology-miner.mjs --doi 10.xxxx/xxxxx');
  console.error('  node ontology-miner.mjs --text "abstract text"');
  console.error('  node ontology-miner.mjs --file ./paper.txt');
  console.error('  Flags: --ontology po|to|both  --provider <name>  --model <name>');
  console.error('  node ontology-miner.mjs --providers   (list all providers)');
  process.exit(1);
}

if (!PROVIDERS[providerKey]) {
  console.error(`Unknown provider "${providerKey}". Run with --providers to list options.`);
  process.exit(1);
}

const providerCfg = PROVIDERS[providerKey];
const model = modelArg ?? providerCfg.defaultModel;

// Validate required env vars before any network I/O
if (providerCfg.envKey && !process.env[providerCfg.envKey]) {
  console.error(`Error: ${providerCfg.envKey} is not set.`);
  process.exit(1);
}
if (providerKey === 'openai-compat' && !process.env.OPENAI_COMPAT_BASE_URL) {
  console.error('Error: OPENAI_COMPAT_BASE_URL is not set.');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Publication fetch
// ---------------------------------------------------------------------------

async function fetchByDoi(doi) {
  console.log(`Fetching metadata for DOI: ${doi}`);

  const crUrl = `https://api.crossref.org/works/${encodeURIComponent(doi)}`;
  const crRes = await fetch(crUrl, {
    headers: { 'User-Agent': 'ontology-miner/1.0 (mailto:research@example.com)' },
  });
  if (!crRes.ok) throw new Error(`CrossRef returned ${crRes.status}`);
  const work = (await crRes.json()).message;

  const title   = work.title?.[0] ?? '(no title)';
  const authors = (work.author ?? []).map(a => `${a.given ?? ''} ${a.family ?? ''}`.trim()).join(', ');
  const journal = work['container-title']?.[0] ?? '';
  const year    = work.published?.['date-parts']?.[0]?.[0] ?? '';
  let abstract  = (work.abstract ?? '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

  if (!abstract) {
    console.log('  No abstract in CrossRef — trying Europe PMC...');
    const pmcUrl = `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=${encodeURIComponent('doi:' + doi)}&resultType=core&format=json`;
    const pmcRes = await fetch(pmcUrl);
    if (pmcRes.ok) {
      abstract = (await pmcRes.json()).resultList?.result?.[0]?.abstractText ?? '';
    }
  }

  if (!abstract) {
    console.warn('  Warning: no abstract found — LLM will work from title only.');
    console.warn('  For better results, use --text to paste the full abstract.');
  }

  return {
    source: 'doi', doi, title, authors, journal, year,
    text: [
      `Title: ${title}`,
      authors ? `Authors: ${authors}` : '',
      journal ? `Journal: ${journal}${year ? ` (${year})` : ''}` : '',
      abstract ? `\nAbstract:\n${abstract}` : '\n(No abstract available — title only)',
    ].filter(Boolean).join('\n'),
  };
}

async function getPublication() {
  if (doiArg)  return fetchByDoi(doiArg);
  if (fileArg) {
    try {
      return { source: 'file', file: fileArg, text: readFileSync(fileArg, 'utf8') };
    } catch (e) {
      console.error(`Error: could not read file "${fileArg}": ${e.message}`);
      process.exit(1);
    }
  }
  return { source: 'text', text: textArg };
}

// ---------------------------------------------------------------------------
// Shared prompt builder
// ---------------------------------------------------------------------------

const ONTOLOGY_CONTEXT = {
  po: `The Plant Ontology (PO) covers plant anatomy (structures: leaf, root, stem, epidermis),
morphology (shapes, surfaces, arrangements), and developmental stages (germination,
flowering, senescence). Terms describe physical structures or temporal stages.
Examples: "abaxial epidermis", "lateral root cap", "inflorescence meristem", "seed germination stage".`,

  to: `The Trait Ontology (TO) covers measurable or observable plant traits and phenotypes:
yield traits, stress tolerance, morphological, biochemical, and agronomic traits.
Terms describe heritable characteristics.
Examples: "drought tolerance", "leaf rolling", "tiller number", "grain protein content".`,

  both: `The Plant Ontology (PO) covers plant anatomy (structures: leaf, root, stem),
morphology (shapes, surfaces), and developmental stages. The Trait Ontology (TO)
covers measurable/observable plant traits and phenotypes: yield, stress tolerance,
morphological, biochemical, and agronomic traits.`,
};

function buildPrompts(publicationText, targetOntology) {
  const ontCtx = ONTOLOGY_CONTEXT[targetOntology] ?? ONTOLOGY_CONTEXT.both;
  const targetLabel = targetOntology === 'both'
    ? 'Plant Ontology (PO) and/or Trait Ontology (TO)'
    : targetOntology === 'po' ? 'Plant Ontology (PO)' : 'Trait Ontology (TO)';

  const system = `You are an expert plant biologist and ontology curator with deep knowledge of
the Plant Ontology (PO) and Trait Ontology (TO). You help curators identify candidate
terms from scientific literature that could enrich these ontologies.

${ontCtx}

When identifying candidate terms:
- Focus on specific, well-defined biological concepts (not vague or generic terms)
- Prefer terms that name a distinct entity, structure, stage, or trait not yet captured
- Include terms used consistently in the paper (not one-off descriptors)
- Synonyms are valuable — note them alongside the primary term
- A term that refines an existing concept (a narrower child term) is as valuable as a new branch`;

  const user = `Analyze this plant science publication and identify candidate terms for ${targetLabel}.

PUBLICATION:
${publicationText}

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
Do not include generic terms already well-established in PO/TO.`;

  return { system, user };
}

function extractJson(raw) {
  // 1. Try fenced code block (non-greedy — correct by construction)
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenced?.[1]) {
    try { return JSON.parse(fenced[1].trim()); } catch { /* fall through */ }
  }
  // 2. Walk each '[' position and bracket-match to find the first valid JSON array
  let start = 0;
  while (true) {
    const i = raw.indexOf('[', start);
    if (i === -1) break;
    let depth = 0, j = i;
    for (; j < raw.length; j++) {
      if (raw[j] === '[') depth++;
      else if (raw[j] === ']') { if (--depth === 0) break; }
    }
    if (depth === 0) {
      try { return JSON.parse(raw.slice(i, j + 1)); } catch { /* try next position */ }
    }
    start = i + 1;
  }
  return [{ _raw: raw }];
}

// ---------------------------------------------------------------------------
// Provider adapters
// ---------------------------------------------------------------------------

async function callAnthropic(system, user) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': process.env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model,
      max_tokens: 4096,
      system,
      messages: [{ role: 'user', content: user }],
    }),
  });
  if (!res.ok) throw new Error(`Anthropic API ${res.status}: ${await res.text()}`);
  return (await res.json()).content?.[0]?.text ?? '';
}

async function callOpenAICompat(baseUrl, apiKey, system, user) {
  const headers = { 'content-type': 'application/json' };
  if (apiKey) headers['authorization'] = `Bearer ${apiKey}`;

  const res = await fetch(`${baseUrl}/v1/chat/completions`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      model,
      max_tokens: 4096,
      messages: [
        { role: 'system', content: system },
        { role: 'user',   content: user   },
      ],
    }),
  });
  if (!res.ok) throw new Error(`${baseUrl} API ${res.status}: ${await res.text()}`);
  return (await res.json()).choices?.[0]?.message?.content ?? '';
}

async function callGemini(system, user) {
  const apiKey = process.env.GEMINI_API_KEY;
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${apiKey}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      system_instruction: { parts: [{ text: system }] },
      contents: [{ role: 'user', parts: [{ text: user }] }],
      generationConfig: { maxOutputTokens: 4096 },
    }),
  });
  if (!res.ok) throw new Error(`Gemini API ${res.status}: ${await res.text()}`);
  return (await res.json()).candidates?.[0]?.content?.parts?.[0]?.text ?? '';
}

async function callLLM(system, user) {
  switch (providerKey) {
    case 'anthropic':
      return callAnthropic(system, user);
    case 'openai':
      return callOpenAICompat('https://api.openai.com', process.env.OPENAI_API_KEY, system, user);
    case 'gemini':
      return callGemini(system, user);
    case 'mistral':
      return callOpenAICompat('https://api.mistral.ai', process.env.MISTRAL_API_KEY, system, user);
    case 'ollama':
      return callOpenAICompat('http://localhost:11434', null, system, user);
    case 'openai-compat': {
      const base = process.env.OPENAI_COMPAT_BASE_URL;
      if (!base) throw new Error('OPENAI_COMPAT_BASE_URL is not set.');
      return callOpenAICompat(base, process.env.OPENAI_COMPAT_API_KEY ?? null, system, user);
    }
    default:
      throw new Error(`Unknown provider: ${providerKey}`);
  }
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function slugify(str) {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
}

function printSummary(candidates) {
  const byConf = { high: [], medium: [], low: [] };
  for (const c of candidates) (byConf[c.confidence] ?? byConf.low).push(c);

  console.log(`\n${'─'.repeat(60)}`);
  console.log(`Candidate terms found: ${candidates.length}  (high: ${byConf.high.length}  medium: ${byConf.medium.length}  low: ${byConf.low.length})`);
  console.log('─'.repeat(60));

  for (const c of [...byConf.high, ...byConf.medium, ...byConf.low]) {
    const badge = c.confidence === 'high' ? '●' : c.confidence === 'medium' ? '◐' : '○';
    console.log(`\n${badge} [${c.ontology ?? '?'}] ${c.term}`);
    if (c.namespace)        console.log(`  Namespace : ${c.namespace}`);
    if (c.suggested_parent) console.log(`  Parent    : ${c.suggested_parent}`);
    if (c.definition_draft) console.log(`  Def       : ${c.definition_draft}`);
    if (c.synonyms?.length) console.log(`  Synonyms  : ${c.synonyms.join(', ')}`);
    if (c.rationale)        console.log(`  Rationale : ${c.rationale}`);
  }
  console.log('\n' + '─'.repeat(60));
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

console.log(`ontology-miner — candidate term extractor for PO/TO`);
console.log(`Provider: ${providerCfg.label}  |  Model: ${model}\n`);

const pub = await getPublication();
console.log(`Source  : ${pub.source}${pub.doi ? ` (${pub.doi})` : ''}`);
if (pub.title) console.log(`Title   : ${pub.title}`);
console.log(`Ontology: ${ontology.toUpperCase()}\n`);

console.log('Sending to LLM for analysis...');
const { system, user } = buildPrompts(pub.text, ontology);
const raw = await callLLM(system, user);
const candidates = extractJson(raw);

if (!candidates.length) {
  console.log('\nNo candidate terms identified in this publication.');
} else if (candidates[0]?._raw) {
  console.log('\nCould not parse LLM response as JSON. Raw output:\n');
  console.log(candidates[0]._raw);
} else {
  printSummary(candidates);
}

// Save
mkdirSync(OUT_DIR, { recursive: true });
const now = new Date();
const date = now.toISOString().slice(0, 10);
const slug = pub.doi
  ? slugify(pub.doi)
  : pub.title
    ? slugify(pub.title)
    : pub.file
      ? slugify(pub.file.split('/').pop().replace(/\.[^.]+$/, ''))
      : slugify((pub.text ?? 'text').slice(0, 50));
const outPath = join(OUT_DIR, `${slug}-${date}.json`);

const realCount = candidates[0]?._raw ? 0 : candidates.length;
writeFileSync(outPath, JSON.stringify({
  mined_at: now.toISOString(),
  provider: providerKey,
  model,
  source: pub.source,
  doi: pub.doi ?? null,
  title: pub.title ?? null,
  ontology_target: ontology,
  candidate_count: realCount,
  candidates: realCount > 0 ? candidates : [],
}, null, 2));

console.log(`\nSaved → ontologies/candidates/${slug}-${date}.json`);
