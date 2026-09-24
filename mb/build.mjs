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
  // callContext intentionally EXCLUDED — its raw dump is ~110 MB and OOMs the
  // Node build (and is too big to ship). Call History uses the live sheet.
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
  // ---- calling: split the event log so the boot payload stays small --------
  // calling.json used to ship all ~120k events (~29 MB). Every tab downloaded
  // and parsed that on load even though only Call View reads the history, and
  // the combined boot payload (~107 MB) was OOM-ing Chrome tabs on lower-memory
  // machines (2026-09-24). So:
  //   calling.json       -> bySeller + meta + the last CALLING_BOOT_DAYS of events
  //   callingEvents.json -> the FULL event array, fetched on demand by the client
  // CALLING_BOOT_DAYS must stay >= the Call View default range (last 7 days,
  // see defaultCallRange() in index.html) or the default view would always
  // trigger the on-demand fetch and we'd gain nothing.
  if (key === 'calling' && Array.isArray(out.events)) {
    const CALLING_BOOT_DAYS = 10;
    const cut = Date.now() - CALLING_BOOT_DAYS * 86400000;
    const all = out.events;
    const recent = all.filter(e => e && e.ts >= cut);
    const fullPath = join(HERE, 'callingEvents.json');
    writeFileSync(fullPath, JSON.stringify({ events: all }, replacer));
    const fb = statSync(fullPath).size;
    manifest.sources.callingEvents = { bytes: fb, events: all.length };
    console.log(`[callingEvents ] ${(fb / 1e6).toFixed(2)} MB (${all.length} events, on-demand)`);
    // Record the EXACT cutoff rather than letting the client re-derive it.
    // The client used to estimate it as "today 00:00 local minus N days", which
    // is not the same instant as this rolling cutoff — in a UTC+ timezone the
    // estimate lands earlier than the real cutoff, so a range that looked
    // in-window would silently render from a truncated event list.
    out = { ...out, events: recent, eventsFrom: cut,
            eventsWindowDays: CALLING_BOOT_DAYS, eventsTotal: all.length };
  }

  const json = JSON.stringify(out, replacer);
  const outPath = join(HERE, `${key}.json`);
  writeFileSync(outPath, json);
  const b = statSync(outPath).size;
  totalBytes += b;
  manifest.sources[key] = { bytes: b };
  console.log(`[${key.padEnd(13)}] ${(b / 1e6).toFixed(2)} MB`);
}

// SAFETY: never blank the manifest. The dashboard only loads mb/*.json for keys
// listed here, so an empty manifest silently disables every Metabase feed and
// drops the whole dashboard back to the Google-Sheets path. That happens if this
// script is run without mb/_raw/ populated (the raw CSVs are gitignored), which
// is easy to do by hand. If nothing was produced, leave the committed manifest
// alone and fail loudly instead.
if (!Object.keys(manifest.sources).length) {
  console.error('[manifest] no sources produced — leaving the existing agg-manifest.json untouched.');
  console.error('[manifest] run `python3 mb/snapshot.py` first to populate mb/_raw/.');
  process.exit(1);
}
writeFileSync(join(HERE, 'agg-manifest.json'), JSON.stringify(manifest, null, 2));
console.log(`[total] ${(totalBytes / 1e6).toFixed(2)} MB JSON across ${Object.keys(manifest.sources).length} sources`);
