// AUTO-COPIED VERBATIM from ../index.html parsers — keep in sync. Do not edit logic here.

const norm = s => s == null ? "" : String(s).replace(/\s+/g, " ").trim();
function num(s) {
  if (s == null || s === "") return null;
  const v = String(s).replace(/,/g,"").trim();
  if (!v) return null;
  const lower = v.toLowerCase();
  if (lower === "did not run" || lower === "not live yet" || lower === "no mapping found") return null;
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}
function parseDate(s) {
  s = norm(s);
  if (!s) return null;
  const lower = s.toLowerCase();
  if (lower === "did not run" || lower === "not live yet") return null;
  let m = s.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})(?:[ ,T]+(\d{1,2}):(\d{2}))?/);
  if (m) {
    let y = parseInt(m[3]); if (y < 100) y += 2000;
    const d = new Date(y, parseInt(m[2]) - 1, parseInt(m[1]), m[4] ? parseInt(m[4]) : 0, m[5] ? parseInt(m[5]) : 0);
    return isNaN(d.getTime()) ? null : d;
  }
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return new Date(parseInt(m[1]), parseInt(m[2])-1, parseInt(m[3]));
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}
const daysBetween = (a, b) => (!a || !b) ? null : Math.floor((b.getTime() - a.getTime()) / 86400000);

// ISO week of a Date — returns { year, week } per ISO-8601
function isoWeek(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const weekNum = Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
  return { year: d.getUTCFullYear(), week: weekNum };
}
const weekKey = (yr, wk) => `${yr}${String(wk).padStart(2,'0')}`;  // e.g. "202620"

function localISODate(d) {
  if (!d || !(d instanceof Date) || isNaN(d.getTime())) return null;
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function parseCSV(text) {
  const rows = [];
  let cur = [], field = "", inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"' && text[i+1] === '"') { field += '"'; i++; }
      else if (c === '"') { inQ = false; }
      else field += c;
    } else {
      if (c === '"') inQ = true;
      else if (c === ',') { cur.push(field); field = ""; }
      else if (c === '\r') {}
      else if (c === '\n') { cur.push(field); rows.push(cur); cur = []; field = ""; }
      else field += c;
    }
  }
  if (field.length || cur.length) { cur.push(field); rows.push(cur); }
  return rows;
}

function processCallingCSV(text) {
  // Positional schema (no header dependency — robust against empty header rows):
  //   A=seller_id, B=call_id, C=call_trigger_interface, D=call_date,
  //   E=call_from, F=call_to, G=gm_name, H=status, I=duration,
  //   J=actionables, K=total_calls_to_seller
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Calling CSV too short (" + rows.length + " rows)");

  const today = new Date(); today.setHours(0,0,0,0);
  const yest = new Date(today.getTime() - 86400000);
  const w3 = new Date(today.getTime() - 2 * 86400000);
  const w5 = new Date(today.getTime() - 4 * 86400000);
  const w7 = new Date(today.getTime() - 6 * 86400000);

  const agg = {};
  const events = [];           // every call event — used for date-range summary
  let totalRows = 0, minD = null, maxD = null;
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 2) continue;
    const sid = norm(r[0]);
    if (!sid) continue;
    const callId       = norm(r[1] || "");
    const triggerIface = norm(r[2] || "");
    const created      = parseDate(r[3]);
    if (!created) continue;
    const gmName       = norm(r[6] || "");
    const status       = norm(r[7] || "").toUpperCase();
    const duration     = parseFloat(r[8]) || 0;
    const actionables  = norm(r[9] || "");
    const totalSellerK = parseInt(r[10], 10) || 0;

    totalRows++;
    if (!minD || created < minD) minD = created;
    if (!maxD || created > maxD) maxD = created;

    const isConn = (status === "CONNECTED" || status === "CONNECT" || status === "ANSWERED");

    // Event row for date-range summary + seller drilldown popup
    events.push({
      sid,
      ts: created.getTime(),
      conn: isConn ? 1 : 0,
      dur: duration || 0,
      gm: gmName,
      status,
      act: actionables,
      cid: callId,
      trg: triggerIface,
    });

    if (!agg[sid]) agg[sid] = {
      total:0, connected:0, disconnected:0, talk_sec:0,
      latest:null, last_call_id:null, last_trigger:null, last_gm:null,
      last_duration:null, last_actionables:null, last_status:null,
      today:0, yesterday:0, last3d:0, last5d:0, last7d:0,
      total_seller_calls:0,
    };
    const a = agg[sid];
    a.total++;
    if (isConn) { a.connected++; a.talk_sec += duration; }
    else        { a.disconnected++; }
    if (!a.latest || created > a.latest) {
      a.latest = created;
      a.last_call_id   = callId;
      a.last_trigger   = triggerIface;
      a.last_gm        = gmName;
      a.last_duration  = duration;
      a.last_actionables = (actionables && actionables.toLowerCase() !== "null") ? actionables : null;
      a.last_status    = status;
    }
    if (totalSellerK) a.total_seller_calls = totalSellerK;
    const dayMs = new Date(created.getFullYear(), created.getMonth(), created.getDate()).getTime();
    if (dayMs === today.getTime()) a.today++;
    if (dayMs === yest.getTime())  a.yesterday++;
    if (dayMs >= w3.getTime())     a.last3d++;
    if (dayMs >= w5.getTime())     a.last5d++;
    if (dayMs >= w7.getTime())     a.last7d++;
  }
  return {
    bySeller: agg,
    events: events,
    meta: {
      total_rows: totalRows,
      unique_sellers: Object.keys(agg).length,
      today: localISODate(today),
      date_range: minD && maxD ? [localISODate(minD), localISODate(maxD)] : [null, null],
    }
  };
}

function processExperimentalCSV(text) {
  // Positional: A=seller_id. Returns array of seller IDs flagged as experimental.
  // (Array — not Set — so it survives JSON cache serialization. A Set is rebuilt
  // on demand in isExperimentalSeller().)
  const rows = parseCSV(text);
  if (rows.length < 1) return { sellerIds: [], count: 0 };
  const seen = new Set();
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 1) continue;
    const sid = norm(r[0]);
    if (sid) seen.add(sid);
  }
  return { sellerIds: Array.from(seen), count: seen.size };
}

function processDailyARRCSV(text) {
  // Positional schema (user spec):
  //   A=seller_id, B=date, I=Total Spend, L=ARR
  // Returns: { bySeller: { sid: { "YYYY-MM-DD": { spend, arr }, ... } }, count, date_range }
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Daily ARR CSV too short (" + rows.length + " rows)");
  const bySeller = {};
  let dateMin = null, dateMax = null;
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 2) continue;
    const sid = norm(r[0]);
    if (!sid) continue;
    const d = parseDate(r[1]);
    if (!d) continue;
    const dateKey = localISODate(d);
    if (!dateMin || dateKey < dateMin) dateMin = dateKey;
    if (!dateMax || dateKey > dateMax) dateMax = dateKey;
    const spend = num(r[8]);   // column I
    const arr   = num(r[11]);  // column L
    if (!bySeller[sid]) bySeller[sid] = {};
    bySeller[sid][dateKey] = { spend, arr };
  }
  return { bySeller, count: Object.keys(bySeller).length, date_range: [dateMin, dateMax] };
}

function processGCV3DumpCSV(text) {
  // GCV3 Dump — positional schema (per user spec):
  //   B=seller_id, C=yearweek, G=AOV, H=cancellation, I=RTO%, J=NMV,
  //   K=logistics_spend, L=marketing_spend, M=COGS, N=spend_gmv, R=PNL%
  // Returns: bySeller[sid] = [{ yearweek, aov, cancellation, rto, nmv, logistics, marketing, cogs, spend_gmv, pnl }, ...]
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("GCV3 Dump CSV too short (" + rows.length + " rows)");
  const bySeller = {};
  let dropped = 0;
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 18) { dropped++; continue; }
    const sid = norm(r[1]);
    if (!sid) { dropped++; continue; }
    const yearweek = norm(r[2]);
    if (!yearweek) { dropped++; continue; }
    const entry = {
      yearweek,
      aov:          num(r[6]),
      cancellation: num(r[7]),
      rto:          num(r[8]),
      nmv:          num(r[9]),
      logistics:    num(r[10]),
      marketing:    num(r[11]),
      cogs:         num(r[12]),
      spend_gmv:    num(r[13]),
      pnl:          num(r[17]),  // column R = index 17
    };
    if (!bySeller[sid]) bySeller[sid] = [];
    bySeller[sid].push(entry);
  }
  // Sort each seller's weeks ascending so consecutive-week logic works
  Object.keys(bySeller).forEach(sid => {
    bySeller[sid].sort((a, b) => a.yearweek.localeCompare(b.yearweek));
  });
  return { bySeller, count: Object.keys(bySeller).length, dropped };
}

function processDailyMetricsCSV(text) {
  // Daily Metrics Tracker — positional schema:
  //   A=seller_id, B=date, C=spend, F=CPM, G=CTR, H=orders, J=spend_gmv (weekly),
  //   K=Last Lifetime PQ Score, L=Last 15 days PQ Score
  // Returns: bySeller[sid] = [{ date, ..., pq_lifetime, pq_15d }, ...]
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Daily Metrics CSV too short (" + rows.length + " rows)");
  const bySeller = {};
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 2) continue;
    const sid = norm(r[0]);
    if (!sid) continue;
    const d = parseDate(r[1]);
    if (!d) continue;
    const dateIso = localISODate(d);
    const entry = {
      date:        norm(r[1]),
      date_iso:    dateIso,
      ts:          d.getTime(),
      spend:       num(r[2]),
      cpm:         num(r[5]),
      ctr:         num(r[6]),
      orders:      num(r[7]),
      spend_gmv:   num(r[9]),
      pq_lifetime: num(r[10]),  // column K — Last Lifetime PQ Score
      pq_15d:      num(r[11]),  // column L — Last 15 days PQ Score
    };
    if (!bySeller[sid]) bySeller[sid] = [];
    bySeller[sid].push(entry);
  }
  // Sort each seller's days ascending
  Object.keys(bySeller).forEach(sid => {
    bySeller[sid].sort((a, b) => a.ts - b.ts);
  });
  return { bySeller, count: Object.keys(bySeller).length };
}

function processCallContextCSV(text) {
  // Call Context Summary Dump — positional schema:
  //   A=seller_id, E=call_date, H=call_by, J=call_status, K=call_duration,
  //   L=recording_link, M=summary, N=next_actionables
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Call Context CSV too short (" + rows.length + " rows)");
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 1) continue;
    const seller_id = norm(r[0]);
    if (!seller_id) continue;
    const dur = parseFloat(r[10]);
    out.push({
      seller_id,
      call_date:        norm(r[4]  || ""),
      call_by:          norm(r[7]  || ""),
      call_status:      norm(r[9]  || ""),
      call_duration:    isNaN(dur) ? 0 : dur,
      recording_link:   norm(r[11] || ""),
      summary:          norm(r[12] || ""),
      next_actionables: norm(r[13] || ""),
    });
  }
  return { rows: out, count: out.length };
}

function processTasksCSV(text) {
  // Column mapping (user-provided, May 2026 schema):
  //   A=Task ID, B=Seller ID, D=Subtype, G=Created Date, H=Due Date, I=Owner, M=Status
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Tasks CSV too short (" + rows.length + " rows)");
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 2) continue;
    const task_id = norm(r[0]);
    if (!task_id) continue;  // skip blank rows
    out.push({
      task_id,
      seller_id:    norm(r[1]),
      subtype:      norm(r[3]  || ""),
      created_date: norm(r[6]  || ""),  // column G
      due_date:     norm(r[7]  || ""),  // column H
      owner:        norm(r[8]  || ""),  // column I
      status:       norm(r[12] || ""),  // column M
    });
  }
  return { rows: out, count: out.length };
}

function processUnassignmentCSV(text) {
  // Positional schema (per user spec):
  //   A=task_id, B=seller_id, E=title, F=status, H=assigned_to,
  //   J=date_created, P=decision, S=request_type, T=week_label
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Unassignment CSV too short (" + rows.length + " rows)");
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 1) continue;
    const task_id = norm(r[0] || "");
    if (!task_id) continue;
    out.push({
      task_id,
      seller_id:   norm(r[1]  || ""),
      title:       norm(r[4]  || ""),
      status:      norm(r[5]  || ""),
      assigned_to: norm(r[7]  || ""),
      date_created:norm(r[9]  || ""),
      decision:    norm(r[15] || ""),
      request_type:norm(r[18] || ""),
      week:        norm(r[19] || ""),
    });
  }
  return { rows: out, count: out.length };
}

function processChatReplyCSV(text) {
  // Positional schema (per user spec): A=seller_id, C=last_gc_reply_datetime.
  // We don't need a header row — skip whatever's in row 1 and consume row 2+.
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Chat Reply CSV too short (" + rows.length + " rows)");
  const out = {};   // seller_id -> { last_gc_reply: Date|null, raw: string }
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 1) continue;
    const seller_id = norm(r[0]);
    if (!seller_id) continue;
    const raw = norm(r[2] || "");
    const d   = parseDate(raw);
    // Keep the most-recent reply if a seller appears multiple times
    if (!out[seller_id] || (d && (!out[seller_id].last_gc_reply || d > out[seller_id].last_gc_reply))) {
      out[seller_id] = { last_gc_reply: d, raw };
    }
  }
  return { bySeller: out, count: Object.keys(out).length };
}

function processTroubleshootCSV(text) {
  // Column mapping (user-provided): A=Seller ID, B=Seller Name, C=Total T/S done,
  // D=Last T/S date, E=T/S Type, F=T/S Actions, H=last_7_days__meta_spend_w_tax.
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("Troubleshoot CSV too short (" + rows.length + " rows)");
  const out = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    if (!r || r.length < 2) continue;
    const seller_id = norm(r[0]);
    if (!seller_id) continue;
    out.push({
      seller_id,
      seller_name:    norm(r[1] || ""),
      total_ts:       num(r[2]) || 0,
      last_ts_date:   norm(r[3] || ""),
      ts_type:        norm(r[4] || ""),
      ts_actions:     norm(r[5] || ""),
      last_7d_spend:  num(r[7]) || 0,
    });
  }
  return { rows: out, count: out.length };
}

export { norm, num, parseDate, daysBetween, isoWeek, weekKey, localISODate, parseCSV, processCallingCSV, processExperimentalCSV, processDailyARRCSV, processGCV3DumpCSV, processDailyMetricsCSV, processCallContextCSV, processTasksCSV, processUnassignmentCSV, processChatReplyCSV, processTroubleshootCSV };
