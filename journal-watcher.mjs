#!/usr/bin/env node
/**
 * journal-watcher.mjs — Scheduled journal scanner for ontology term discovery
 *
 * Reads journals.yml, polls CrossRef for new papers published since the last
 * scan, deduplicates against scan history, and feeds each new paper to
 * ontology-agent.mjs for agentic term mining.
 *
 * Usage:
 *   node journal-watcher.mjs              # scan now using journals.yml
 *   node journal-watcher.mjs --dry-run    # show what would be processed, no LLM calls
 *   node journal-watcher.mjs --cron       # print crontab line for automated scheduling
 *   node journal-watcher.mjs --status     # show scan history summary
 *
 * To schedule (examples):
 *   crontab -e  →  paste the output of --cron
 *   Or use the /schedule skill in Claude Code for managed scheduling.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { spawn } from 'child_process';

const ROOT         = dirname(fileURLToPath(import.meta.url));
const HISTORY_FILE = join(ROOT, 'ontologies', 'scan-history.json');
const JOURNALS_FILE = join(ROOT, 'journals.yml');

try { const { config } = await import('dotenv'); config(); } catch { /* optional */ }

// ---------------------------------------------------------------------------
// Args
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);
const DRY_RUN = args.includes('--dry-run');

if (args.includes('--cron')) {
  const scriptPath = join(ROOT, 'journal-watcher.mjs');
  const logPath    = join(ROOT, 'ontologies', 'watcher.log');
  console.log('\n# Add one of these to your crontab (crontab -e):\n');
  console.log(`# Every day at 8am:`);
  console.log(`0 8 * * * node ${scriptPath} >> ${logPath} 2>&1`);
  console.log(`\n# Every Monday at 9am:`);
  console.log(`0 9 * * 1 node ${scriptPath} >> ${logPath} 2>&1`);
  console.log(`\n# Every 3 days at 8am:`);
  console.log(`0 8 */3 * * node ${scriptPath} >> ${logPath} 2>&1`);
  console.log('\n# Or use Claude Code\'s /schedule skill for managed scheduling.');
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

let yaml;
try {
  const { load } = await import('js-yaml');
  yaml = load;
} catch {
  console.error('Error: js-yaml is required. Run: npm install');
  process.exit(1);
}

if (!existsSync(JOURNALS_FILE)) {
  console.error(`journals.yml not found. Copy journals.example.yml → journals.yml and customize it.`);
  process.exit(1);
}

const config = yaml(readFileSync(JOURNALS_FILE, 'utf8'));
const rawJournals = config.journals ?? [];
const scanCfg     = config.scan ?? {};
const lookbackDays  = scanCfg.lookback_days ?? 7;
const maxPerJournal = scanCfg.max_per_journal ?? 20;
const provider    = scanCfg.provider ?? process.env.LLM_PROVIDER ?? 'anthropic';
const modelFlag   = scanCfg.model ? ['--model', scanCfg.model] : [];
const ontology    = scanCfg.ontology ?? 'both';

// Validate journal entries — skip those with missing ISSN, fix invalid focus values
const VALID_FOCUS = new Set(['po', 'to', 'both']);
const journals = rawJournals.filter((j, idx) => {
  if (!j.issn) {
    console.warn(`journals.yml: entry #${idx + 1} ("${j.name ?? 'unnamed'}") is missing 'issn' — skipped`);
    return false;
  }
  if (j.focus && !VALID_FOCUS.has(j.focus)) {
    console.warn(`journals.yml: "${j.name ?? j.issn}" has invalid focus "${j.focus}" (must be po, to, or both) — defaulting to 'both'`);
    j.focus = 'both';
  }
  return true;
});

// ---------------------------------------------------------------------------
// History management
// ---------------------------------------------------------------------------

const MAX_FAILURES = 3; // stop retrying a DOI after this many agent failures

function loadHistory() {
  if (!existsSync(HISTORY_FILE)) return { last_scan: null, processed_dois: {}, failed_dois: {} };
  try {
    const h = JSON.parse(readFileSync(HISTORY_FILE, 'utf8'));
    h.failed_dois ??= {};
    return h;
  }
  catch { return { last_scan: null, processed_dois: {}, failed_dois: {} }; }
}

function saveHistory(history) {
  mkdirSync(dirname(HISTORY_FILE), { recursive: true });
  writeFileSync(HISTORY_FILE, JSON.stringify(history, null, 2));
}

function markProcessed(history, doi) {
  history.processed_dois[doi.toLowerCase()] = new Date().toISOString();
}

function markFailed(history, doi) {
  const key = doi.toLowerCase();
  history.failed_dois[key] = (history.failed_dois[key] ?? 0) + 1;
}

function isProcessed(history, doi) {
  return !!history.processed_dois[doi.toLowerCase()];
}

function isBlocked(history, doi) {
  return (history.failed_dois[doi.toLowerCase()] ?? 0) >= MAX_FAILURES;
}

// ---------------------------------------------------------------------------
// --status
// ---------------------------------------------------------------------------

if (args.includes('--status')) {
  const history = loadHistory();
  const total = Object.keys(history.processed_dois).length;
  console.log(`Scan history: ${HISTORY_FILE}`);
  console.log(`Last scan   : ${history.last_scan ?? 'never'}`);
  console.log(`Total DOIs  : ${total}`);
  if (total) {
    const recent = Object.entries(history.processed_dois)
      .sort((a, b) => b[1].localeCompare(a[1]))
      .slice(0, 5);
    console.log(`\nMost recent:`);
    for (const [doi, ts] of recent) console.log(`  ${ts.slice(0, 10)}  ${doi}`);
  }
  console.log(`\nJournals configured: ${journals.length}`);
  for (const j of journals) console.log(`  ${j.issn}  ${j.name}`);
  process.exit(0);
}

// ---------------------------------------------------------------------------
// CrossRef polling
// ---------------------------------------------------------------------------

function toISODate(daysAgo) {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  return d.toISOString().slice(0, 10);
}

async function fetchNewPapers(journal) {
  const fromDate = toISODate(lookbackDays);
  const filter   = `issn:${journal.issn},from-pub-date:${fromDate}`;
  const select   = 'DOI,title,published,abstract,author,container-title';
  const url      = `https://api.crossref.org/works?filter=${encodeURIComponent(filter)}&sort=published&order=desc&rows=${maxPerJournal}&select=${select}`;

  const res = await fetch(url, {
    headers: { 'User-Agent': 'journal-watcher/1.0 (mailto:research@example.com)' },
  });

  if (!res.ok) {
    console.warn(`  CrossRef ${res.status} for ${journal.name} (${journal.issn})`);
    return [];
  }

  const items = (await res.json()).message?.items ?? [];
  return items.map(item => ({
    doi:     item.DOI,
    title:   item.title?.[0] ?? '(no title)',
    journal: item['container-title']?.[0] ?? journal.name,
    year:    item.published?.['date-parts']?.[0]?.[0] ?? '',
    hasAbstract: !!(item.abstract),
  }));
}

// ---------------------------------------------------------------------------
// Run agent on a single paper
// ---------------------------------------------------------------------------

function runAgent(doi, journalOntology) {
  return new Promise((resolve, reject) => {
    const targetOntology = journalOntology ?? ontology;
    const agentArgs = ['ontology-agent.mjs', '--doi', doi, '--provider', provider, '--ontology', targetOntology, ...modelFlag];

    console.log(`    node ${agentArgs.join(' ')}`);
    if (DRY_RUN) { resolve({ skipped: true }); return; }

    const child = spawn('node', agentArgs, { cwd: ROOT, stdio: 'inherit' });
    child.on('exit', code => {
      if (code === 0) resolve({ doi, success: true });
      else reject(new Error(`Agent exited ${code} for ${doi}`));
    });
    child.on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// Main scan loop
// ---------------------------------------------------------------------------

const history = loadHistory();
const startTime = new Date();

console.log(`journal-watcher — ${DRY_RUN ? '[DRY RUN] ' : ''}scanning ${journals.length} journal(s)`);
console.log(`Lookback: ${lookbackDays} days  |  Provider: ${provider}  |  Ontology: ${ontology}\n`);

let totalNew = 0;
let totalProcessed = 0;

for (const journal of journals) {
  process.stdout.write(`Scanning: ${journal.name} (${journal.issn}) ... `);

  let papers;
  try {
    papers = await fetchNewPapers(journal);
  } catch (e) {
    console.log(`ERROR: ${e.message}`);
    continue;
  }

  const newPapers = papers.filter(p => !isProcessed(history, p.doi) && !isBlocked(history, p.doi));
  console.log(`${papers.length} recent, ${newPapers.length} new`);
  totalNew += newPapers.length;

  for (const paper of newPapers) {
    console.log(`\n  DOI   : ${paper.doi}`);
    console.log(`  Title : ${paper.title}`);
    console.log(`  ${paper.hasAbstract ? '✓ abstract available' : '⚠ no abstract — title only'}`);

    try {
      await runAgent(paper.doi, journal.focus);
      if (!DRY_RUN) {
        markProcessed(history, paper.doi);
        totalProcessed++;
      }
    } catch (e) {
      console.warn(`  ⚠ Agent failed: ${e.message}`);
      if (!DRY_RUN) {
        markFailed(history, paper.doi);
        const failures = history.failed_dois[paper.doi.toLowerCase()];
        if (failures >= MAX_FAILURES) console.warn(`  ✗ Blocked after ${MAX_FAILURES} failures — will not retry`);
      }
    }

    // Save history after each paper so progress isn't lost on error
    history.last_scan = new Date().toISOString();
    saveHistory(history);
  }
}

history.last_scan = new Date().toISOString();
saveHistory(history);

const elapsed = ((new Date() - startTime) / 1000).toFixed(1);
console.log(`\n${'─'.repeat(60)}`);
console.log(`Scan complete in ${elapsed}s`);
console.log(`New papers found : ${totalNew}`);
console.log(DRY_RUN
  ? `Would have processed: ${totalNew} (dry run — no LLM calls made)`
  : `Processed          : ${totalProcessed}`);
console.log(`History saved    : ${HISTORY_FILE}`);
console.log(`\nRun with --status to see history. Use --cron to set up automated scheduling.`);
