#!/usr/bin/env node
/**
 * export-candidates.mjs — Convert mined candidates to ontology submission formats
 *
 * Reads a candidates JSON file produced by ontology-miner.mjs or ontology-agent.mjs
 * and exports submission-ready files for ontology curators.
 *
 * Output formats:
 *   obo      — OBO stanza blocks, ready to paste into a .obo file or GitHub PR
 *   robot    — ROBOT template TSV, importable with `robot template`
 *   github   — GitHub New Term Request (NTR) markdown, one file per term
 *   csv      — Flat CSV for spreadsheet-based human review
 *   all      — All of the above (default)
 *
 * Usage:
 *   node export-candidates.mjs --input ontologies/candidates/my-paper-2026-06-05.json
 *   node export-candidates.mjs --input ontologies/candidates/my-paper-2026-06-05.json --format obo
 *   node export-candidates.mjs --input ontologies/candidates/my-paper-2026-06-05.json --format github
 *   node export-candidates.mjs --list   (show all available candidate files)
 *
 * Submission targets:
 *   PO  → https://github.com/Planteome/plant-ontology/issues/new
 *   TO  → https://github.com/Planteome/plant-trait-ontology/issues/new
 *   PECO→ https://github.com/Planteome/plant-experimental-conditions-ontology/issues/new
 */

import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from 'fs';
import { join, dirname, basename, extname } from 'path';
import { fileURLToPath } from 'url';

const ROOT    = dirname(fileURLToPath(import.meta.url));
const CAND_DIR = join(ROOT, 'ontologies', 'candidates');
const EXP_DIR  = join(ROOT, 'ontologies', 'exports');

// ---------------------------------------------------------------------------
// Args
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);
function getArg(flag) {
  const i = args.indexOf(flag);
  const v = i !== -1 ? args[i + 1] : undefined;
  return v && !v.startsWith('--') ? v : null;
}

if (args.includes('--list')) {
  if (!existsSync(CAND_DIR)) { console.log('No candidates directory found.'); process.exit(0); }
  const files = readdirSync(CAND_DIR).filter(f => f.endsWith('.json') && f !== '.gitkeep');
  if (!files.length) { console.log('No candidate files found in ontologies/candidates/'); process.exit(0); }
  console.log(`Candidate files in ontologies/candidates/:\n`);
  for (const f of files.sort().reverse()) {
    const data = JSON.parse(readFileSync(join(CAND_DIR, f), 'utf8'));
    console.log(`  ${f}  (${data.candidate_count ?? 0} candidates)`);
  }
  process.exit(0);
}

const inputArg  = getArg('--input');
const formatArg = getArg('--format') ?? 'all';

if (!inputArg) {
  console.error('Usage: node export-candidates.mjs --input <candidates-json> [--format obo|robot|github|csv|all]');
  console.error('       node export-candidates.mjs --list');
  process.exit(1);
}

const inputPath = inputArg.startsWith('/') ? inputArg : join(ROOT, inputArg);
if (!existsSync(inputPath)) {
  console.error(`File not found: ${inputPath}`);
  process.exit(1);
}

const data       = JSON.parse(readFileSync(inputPath, 'utf8'));
const candidates = (data.candidates ?? []).filter(c => !c._raw);
const doi        = data.doi ?? null;
const title      = data.title ?? null;
const minedAt    = data.mined_at ?? new Date().toISOString();

if (!candidates.length) {
  console.log('No candidates found in this file.');
  process.exit(0);
}

const slug     = basename(inputPath, extname(inputPath));
const outBase  = join(EXP_DIR, slug);
mkdirSync(EXP_DIR, { recursive: true });

console.log(`export-candidates — ${candidates.length} candidate(s) from ${basename(inputPath)}\n`);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Ontology namespace → GitHub repo
const GITHUB_REPOS = {
  PO:    'https://github.com/Planteome/plant-ontology/issues/new',
  TO:    'https://github.com/Planteome/plant-trait-ontology/issues/new',
  PECO:  'https://github.com/Planteome/plant-experimental-conditions-ontology/issues/new',
  FLOPO: 'https://github.com/flora-phenotype-ontology/flopoontology/issues/new',
};

// Namespace → OBO ontology prefix
const NAMESPACE_PREFIX = {
  plant_anatomy:             'PO',
  plant_morphology:          'PO',
  plant_developmental_stage: 'PO',
  trait:                     'TO',
};

function termPrefix(c) {
  if (NAMESPACE_PREFIX[c.namespace]) return NAMESPACE_PREFIX[c.namespace];
  if (c.ontology === 'TO') return 'TO';
  if (c.ontology === 'PO') return 'PO';
  return null; // 'either' or unknown — curator must assign namespace
}

// Placeholder ID — curators replace with real accession
function placeholderId(c, index) {
  const prefix = termPrefix(c);
  const num    = String(index + 1).padStart(3, '0');
  return prefix ? `${prefix}:NEWTERM_${num}` : `??:NEWTERM_${num}`;
}

function doiRef(d) {
  return d ? `DOI:${d}` : 'REF:UNKNOWN';
}

function escapeOboDef(s) {
  return s.replace(/"/g, '\\"');
}

// ---------------------------------------------------------------------------
// Format: OBO stanzas
// ---------------------------------------------------------------------------

function buildObo() {
  const lines = [
    `! OBO stanza export`,
    `! Source   : ${inputPath}`,
    `! Paper    : ${title ?? '(unknown)'}`,
    `! DOI      : ${doi ?? '(unknown)'}`,
    `! Mined at : ${minedAt}`,
    `! Note     : Replace NEWTERM_XXX IDs with real accessions before committing.`,
    `!            Review and edit definition_draft before committing.`,
    '',
  ];

  for (const [i, c] of candidates.entries()) {
    const id = placeholderId(c, i);
    lines.push(`[Term]`);
    lines.push(`id: ${id}`);
    lines.push(`name: ${c.term}`);
    if (c.namespace) lines.push(`namespace: ${c.namespace}`);
    if (c.definition_draft) lines.push(`def: "${escapeOboDef(c.definition_draft)}" [${doiRef(doi)}]`);
    if (c.suggested_parent) lines.push(`! suggested_parent: ${c.suggested_parent}  (verify PO/TO accession before using is_a)`);
    for (const syn of c.synonyms ?? []) lines.push(`synonym: "${syn}" EXACT []`);
    if (c.source_sentence) lines.push(`comment: Source: "${c.source_sentence.slice(0, 200).replace(/"/g, '\\"')}"`);
    lines.push(`! confidence: ${c.confidence}`);
    lines.push('');
  }

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Format: ROBOT template TSV
// ---------------------------------------------------------------------------

function buildRobot() {
  // ROBOT template header: https://robot.obolibrary.org/template
  const headers = ['ID', 'Label', 'Definition', 'Parent', 'Exact Synonym', 'Comment'];
  const robotHeaders = [
    'ID',
    'LABEL',
    "A definition 'definition'@en",
    "SC %",                          // subClassOf parent
    "A oboInOwl:hasExactSynonym",
    "A rdfs:comment",
  ];

  const rows = [headers, robotHeaders];

  for (const [i, c] of candidates.entries()) {
    const id = placeholderId(c, i);
    rows.push([
      id,
      c.term,
      c.definition_draft ?? '',
      c.suggested_parent ?? '',
      (c.synonyms ?? []).join('|'),
      [
        c.rationale ?? '',
        doi ? `Source DOI: ${doi}` : '',
        `Confidence: ${c.confidence}`,
        c.source_sentence ? `Evidence: "${c.source_sentence.slice(0, 150)}"` : '',
      ].filter(Boolean).join(' | '),
    ]);
  }

  return rows.map(r => r.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join('\t')).join('\n');
}

// ---------------------------------------------------------------------------
// Format: GitHub NTR issues (one markdown file per term, grouped by ontology)
// ---------------------------------------------------------------------------

function buildGithubIssue(c) {
  const isAmbiguous = !c.ontology || c.ontology === 'either';
  const onto  = c.ontology === 'TO' ? 'TO' : c.ontology === 'PO' ? 'PO' : null;
  const repo  = onto ? GITHUB_REPOS[onto] : null;

  const lines = [
    `## New Term Request — ${onto ?? 'PO or TO (curator: please decide)'}`,
    '',
    `**Proposed label:** ${c.term}`,
    onto ? `**Ontology:** ${onto === 'PO' ? 'Plant Ontology (PO)' : 'Trait Ontology (TO)'}` : `**Ontology:** ⚠ Ambiguous — curator must determine whether this belongs in PO or TO`,
    c.namespace ? `**Namespace:** \`${c.namespace}\`` : null,
    '',
    '### Definition',
    '',
    c.definition_draft ?? '_(draft — please review)_',
    '',
    '### Suggested parent term',
    '',
    c.suggested_parent ? `\`${c.suggested_parent}\`` : '_(see rationale)_',
    '',
  ];

  if (c.synonyms?.length) {
    lines.push('### Synonyms', '');
    for (const s of c.synonyms) lines.push(`- \`${s}\` (EXACT)`);
    lines.push('');
  }

  lines.push(
    '### Evidence',
    '',
    doi   ? `**Paper DOI:** ${doi}` : null,
    title ? `**Paper title:** ${title}` : null,
    '',
    '**Supporting sentence from paper:**',
    '',
    `> ${c.source_sentence ?? '_(not available)_'}`,
    '',
    '### Rationale',
    '',
    c.rationale ?? '_(auto-generated candidate — please review)_',
    '',
    '### Automated check (OLS)',
    '',
    c.ols_search_result
      ? `OLS search result: ${c.ols_search_result}`
      : '_OLS check not recorded_',
    '',
    '---',
    `_Candidate generated by [llm-ontology-miner](https://github.com/jdetras/llm-ontology-miner) · confidence: ${c.confidence}_`,
    '',
    isAmbiguous
      ? `**Curator note:** Ontology is ambiguous — decide before submitting.\n- [PO Issues](${GITHUB_REPOS.PO})\n- [TO Issues](${GITHUB_REPOS.TO})`
      : `**Submit to:** [${onto} GitHub Issues](${repo})`,
  );

  return lines.filter(l => l !== null).join('\n');
}

function buildGithubAll() {
  return candidates.map((c, i) => `<!-- TERM ${i + 1} of ${candidates.length} -->\n\n${buildGithubIssue(c)}`).join('\n\n---\n\n');
}

// ---------------------------------------------------------------------------
// Format: CSV (human review)
// ---------------------------------------------------------------------------

function csvCell(v) {
  const s = String(v ?? '').replace(/"/g, '""');
  return `"${s}"`;
}

function buildCsv() {
  const headers = ['#', 'confidence', 'ontology', 'namespace', 'term', 'definition_draft', 'suggested_parent', 'synonyms', 'source_sentence', 'rationale', 'ols_check', 'doi'];
  const rows = [headers.map(csvCell).join(',')];
  for (const [i, c] of candidates.entries()) {
    rows.push([
      i + 1,
      c.confidence,
      c.ontology,
      c.namespace,
      c.term,
      c.definition_draft,
      c.suggested_parent,
      (c.synonyms ?? []).join('; '),
      c.source_sentence,
      c.rationale,
      c.ols_search_result ?? '',
      doi ?? '',
    ].map(csvCell).join(','));
  }
  return rows.join('\n');
}

// ---------------------------------------------------------------------------
// Write outputs
// ---------------------------------------------------------------------------

const formats = formatArg === 'all' ? ['obo', 'robot', 'github', 'csv'] : [formatArg];
const written = [];

for (const fmt of formats) {
  let content, ext, label;
  switch (fmt) {
    case 'obo':
      content = buildObo();       ext = '.obo';      label = 'OBO stanzas';
      break;
    case 'robot':
      content = buildRobot();     ext = '-robot.tsv'; label = 'ROBOT template TSV';
      break;
    case 'github':
      content = buildGithubAll(); ext = '-ntr.md';    label = 'GitHub NTR markdown';
      break;
    case 'csv':
      content = buildCsv();       ext = '.csv';       label = 'CSV for review';
      break;
    default:
      console.warn(`Unknown format: ${fmt} (use obo | robot | github | csv | all)`);
      continue;
  }
  const outPath = `${outBase}${ext}`;
  writeFileSync(outPath, content, 'utf8');
  written.push({ label, path: outPath });
  console.log(`✓ ${label.padEnd(22)} → ontologies/exports/${basename(outPath)}`);
}

console.log(`\n${candidates.length} candidate(s) exported in ${written.length} format(s).`);

// Print submission links for GitHub NTR
if (formats.includes('github')) {
  const ontos = [...new Set(candidates.flatMap(c =>
    c.ontology === 'TO' ? ['TO'] : c.ontology === 'PO' ? ['PO'] : ['PO', 'TO']
  ))];
  console.log('\nSubmission links:');
  for (const onto of ontos) {
    const repo = GITHUB_REPOS[onto];
    if (repo) console.log(`  ${onto}: ${repo}`);
  }
  const ambiguous = candidates.filter(c => !c.ontology || c.ontology === 'either');
  if (ambiguous.length) console.log(`\n  ⚠ ${ambiguous.length} candidate(s) have ambiguous ontology — see curator note in the NTR file`);
  console.log('\nTip: open the -ntr.md file, copy each term block, and paste into a new GitHub issue.');
}

if (formats.includes('robot')) {
  console.log('\nROBOT import command:');
  console.log(`  robot template --input your-ontology.owl --template ${outBase}-robot.tsv --output new-terms.owl`);
}
