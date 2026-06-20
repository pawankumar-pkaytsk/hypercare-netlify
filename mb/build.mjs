// Aggregation builder for Hypercare Dashboard V2.
//
// Reads the raw Metabase CSVs (written by hypercare_v2_snapshot.py into
// mb/_raw/) and runs the dashboard's OWN parsers (parsers.mjs, copied verbatim)
// to produce compact, view-ready JSON — the exact object each parser returns.
// The dashboard then loads this JSON and skips fetch+parse for these sources.
//
// Date objects in parser output are serialized as {"$date": <epoch ms>} so the
// browser can rehydrate them losslessly.
//
// Run: node mb/build.mjs   (after the Python snapshot has populated mb/_raw/)
import { readFileSync, writeFileSync, existsSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import * as P from './parsers.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const RAW = join(HERE, '_raw');

// key -> parser fn. Order mirrors the dashboard's fetchSheets destructure.
const PARSERS = {
  calling:       P.processCallingCSV,
  tasks:         P.processTasksCSV,
  troubleshoot:  P.processTroubleshootCSV,
  chatReply:     P.processChatReplyCSV,
  unassignment:  P.processUnassignmentCSV,
  dailyARR:      P.processDailyARRCSV,
  experimental:  P.processExperimentalCSV,
  callContext:   P.processCallContextCSV,
  gcv3Dump:      P.processGCV3DumpCSV,
  dailyMetrics:  P.processDailyMetricsCSV,
};

// Serialize Date -> {"$date": epochMs}. JSON.stringify calls Date.toJSON before
// the replacer sees it, so we detect via the original value on `this`.
function replacer(key, value) {
  const orig = this[key];
  if (orig instanceof Date) return { $date: orig.getTime() };
  return value;
}

const manifest = { generatedAt: new Date().toISOString(), sources: {} };
let totalBytes = 0;

for (const [key, fn] of Object.entries(PARSERS)) {
  const csvPath = join(RAW, `${key}.csv`);
  if (!existsSync(csvPath)) {
    console.log(`[${key.padEnd(13)}] no raw CSV — skipped`);
    continue;
  }
  const text = readFileSync(csvPath, 'utf8');
  let out;
  try {
    out = fn(text);
  } catch (e) {
    console.log(`[${key.padEnd(13)}] PARSE ERROR: ${e.message}`);
    manifest.sources[key] = { error: e.message };
    continue;
  }
  const json = JSON.stringify(out, replacer);
  const outPath = join(HERE, `${key}.json`);
  writeFileSync(outPath, json);
  const b = statSync(outPath).size;
  totalBytes += b;
  manifest.sources[key] = { bytes: b };
  console.log(`[${key.padEnd(13)}] ${(b / 1e6).toFixed(2)} MB`);
}

writeFileSync(join(HERE, 'agg-manifest.json'), JSON.stringify(manifest, null, 2));
console.log(`[total] ${(totalBytes / 1e6).toFixed(2)} MB JSON across ${Object.keys(manifest.sources).length} sources`);
