#!/usr/bin/env python3
"""Snapshot 10 Metabase cards → static CSV files for Hypercare Dashboard V2.

Why: the dashboard is static (GitHub Pages). The browser cannot call Metabase
directly (card queries need auth + Metabase sends no CORS headers). So this
script pulls each card server-side with the cached Metabase creds, writes one
CSV per source into ~/hypercarev2new/mb/, and (with --push) commits + pushes so
GitHub Pages redeploys. The dashboard then loads these CSVs from the same origin
with no auth — fast, and the existing positional process*CSV() parsers consume
them unchanged (each card's column order already matches its parser).

Run once:   python3 ~/metabase-arr-refresh/hypercare_v2_snapshot.py
Deploy:     python3 ~/metabase-arr-refresh/hypercare_v2_snapshot.py --push
Creds: ~/metabase-arr-refresh/.mbcreds (JSON) else Claude desktop config
       mcpServers.metabase.env  (same pattern as the other refresh.py scripts).
"""
import json, os, sys, subprocess, urllib.request, datetime, time

REPO = os.environ.get("HC_REPO") or os.path.expanduser("~/hypercare-netlify")  # env override for CI
# Raw CSVs are an INTERMEDIATE artifact (gitignored). The Node builder
# (mb/build.mjs) parses them into compact mb/*.json which is what ships.
OUTDIR = os.path.join(REPO, "mb", "_raw")
CRED_CACHE = os.path.expanduser("~/metabase-arr-refresh/.mbcreds")
DESKTOP_CFG = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")

# key -> Metabase card id. Order/columns of each card already match the
# dashboard's positional parser for that key (verified against the parsers).
CARDS = {
    "calling":       10206,
    "tasks":         10181,
    "troubleshoot":  10189,
    "chatReply":     10363,
    "unassignment":  9353,
    "dailyARR":      10469,
    "experimental":  8684,
    "gcv3Dump":      1880,
    "dailyMetrics":  10773,
}


# ---- Server-side reduction --------------------------------------------------
# Raw dumps total ~225 MB (callContext 102, gcv3 58, tasks 35 ...). The views are
# recency-focused, so we trim each source to a generous window that comfortably
# covers what the UI shows. Tune the windows here if a view needs more history.
# TASK_GC_TOKENS is DERIVED from the roster lists further down (see _task_tokens()).
# It used to be a hand-maintained literal and silently drifted: Tanaya Gore and
# Sargunpreet Singh joined 2026-07 and were added to the dashboard's MKT_GCS but
# never here, so every one of their tasks was dropped from the snapshot and
# Marketing Task View showed 0 tasks for 2 of the 3 active GCs. Deriving it from
# the rosters means adding a GC in one place can no longer lose their tasks.
GCV3_WEEKS = 12   # keep the most recent N distinct year_weeks


def _cutoff(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _recent(rows, field, days):
    cut = _cutoff(days)
    out = []
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        s = str(v)[:10]
        if len(s) == 10 and s[4] == '-':   # looks like ISO YYYY-MM-DD
            if s >= cut:
                out.append(r)
        else:
            out.append(r)   # unparseable date → keep (don't silently drop)
    return out


def reduce_rows(key, rows):
    if not rows:
        return rows
    if key == "calling":
        return _recent(rows, "call_date", 75)
    if key == "dailyMetrics":
        return _recent(rows, "date", 90)
    if key == "tasks":
        rows = _recent(rows, "task_created_at", 150)
        def is_ours(r):
            nm = str(r.get("assignee_name") or "").lower()
            return any(tok in nm for tok in TASK_GC_TOKENS)
        return [r for r in rows if is_ours(r)]
    if key == "gcv3Dump":
        weeks = sorted({str(r.get("year_week")) for r in rows if r.get("year_week") is not None}, reverse=True)
        keep = set(weeks[:GCV3_WEEKS])
        return [r for r in rows if str(r.get("year_week")) in keep]
    # dailyARR (Apr→now, needed in full), experimental, troubleshoot,
    # chatReply, unassignment → keep full (already small).
    return rows


def creds():
    # CI / handsfree: read straight from env (GitHub Actions secrets).
    if os.environ.get("METABASE_URL"):
        return (os.environ["METABASE_URL"].rstrip('/'),
                os.environ.get("METABASE_USER_EMAIL", ""),
                os.environ.get("METABASE_PASSWORD", ""))
    e = json.load(open(CRED_CACHE)) if os.path.exists(CRED_CACHE) \
        else json.load(open(DESKTOP_CFG))['mcpServers']['metabase']['env']
    # cache for next run
    if not os.path.exists(CRED_CACHE):
        try: json.dump(e, open(CRED_CACHE, "w"))
        except Exception: pass
    return e['METABASE_URL'].rstrip('/'), e['METABASE_USER_EMAIL'], e['METABASE_PASSWORD']


def req(url, method='GET', body=None, H=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=H or {})
    with urllib.request.urlopen(r, timeout=600) as resp:
        return json.loads(resp.read().decode())


def csv_escape(v):
    if v is None:
        return ""
    s = str(v)
    if '"' in s or ',' in s or '\n' in s or '\r' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def rows_to_csv(cols, rows):
    out = [",".join(csv_escape(c) for c in cols)]
    for row in rows:
        out.append(",".join(csv_escape(c) for c in row))
    return "\n".join(out)


def _spend_num(v):
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(',', '').strip() or 0)
    except Exception:
        return 0.0


def build_revival_spend(url, H):
    """Revival Compliance feed: per revived seller, post-revival spend =
    max(0, total_spend[card 10065 col 'total spend'] - pre_revival_spend[card
    9532 col 'spend']). Flag = post-revival spend == 0 (didn't start spending).
    Writes the committed mb/revivalSpend.json. Best-effort: on quota/error it
    leaves any previous file untouched."""
    def pull(cid):
        for attempt in range(4):
            try:
                return req(f"{url}/api/card/{cid}/query/json", 'POST', {}, H)
            except Exception as e:
                last = e
                time.sleep(2 + attempt * 3)
        raise last
    try:
        pre_rows = pull(9532)     # unassigned/revived dump — col 'spend' = pre-revival
        tot_rows = pull(10065)    # marketing overall — col 'total spend'
        gc_rows  = pull(7753)     # seller_manager_mapping — post-revival GC assignment
    except Exception as e:
        print(f"[revivalSpend] FAILED: {e} (keeping previous file)")
        return
    # seller_id → post-revival GC (growth_consultant_name; card 7753 idx 9).
    gc_by = {}
    for r in gc_rows:
        sid = str(r.get('seller_id') or '').strip()
        gc = str(r.get('growth_consultant_name') or '').strip()
        if sid and gc:
            gc_by[sid] = gc
    # Dedupe 9532 by seller → most-recent unassignment event (submitted_at/created_at).
    by = {}
    for r in pre_rows:
        sid = str(r.get('seller_id') or '').strip()
        if not sid:
            continue
        k = str(r.get('submitted_at') or r.get('created_at') or '')
        if sid not in by or k >= by[sid]['_k']:
            by[sid] = {'_k': k, 'preRevival': _spend_num(r.get('spend')),
                       'decision': r.get('decision') or '', 'status': r.get('status') or '',
                       'goLive': str(r.get('go_live_date') or '')[:10]}
    tot_by = {}
    for r in tot_rows:
        sid = str(r.get('seller id') or r.get('seller_id') or '').strip()
        if sid:
            tot_by[sid] = {'total': _spend_num(r.get('total spend')),
                           'lastSpend': str(r.get('last spend date') or '')[:10]}
    bySeller, not_started = {}, 0
    for sid, d in by.items():
        t = tot_by.get(sid, {})
        total = t.get('total', 0.0)
        after = total - d['preRevival']
        if after < 0:
            after = 0.0
        started = after > 0
        if not started:
            not_started += 1
        bySeller[sid] = {'preRevival': round(d['preRevival'], 2), 'totalSpend': round(total, 2),
                         'spendAfter': round(after, 2), 'started': started,
                         'decision': d['decision'], 'goLive': d['goLive'],
                         'gc': gc_by.get(sid, ''), 'lastSpend': t.get('lastSpend', '')}
    out = {'generatedAt': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'bySeller': bySeller, 'total': len(bySeller), 'notStarted': not_started}
    path = os.path.join(REPO, "mb", "revivalSpend.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(',', ':'))
    print(f"[revivalSpend] {len(bySeller)} revived sellers · {not_started} did NOT start spending → {path}")
    return bySeller


# ---- Marketing Seller View: scope now sourced from Metabase, not the sheet ----
# Mirror of the dashboard's MKT_GCS. Removed Nishan Bandekar (left org 2026-07);
# added Tanaya Gore + Sargunpreet Singh (joined 2026-07). Names are canonicalized
# (card 7753 growth_consultant_name has stray double spaces).
MKT_GCS = ["Nikita S GC", "Tanaya Gore", "Sargunpreet Singh"]

# Some GCs appear under name variants in card 7753 → collapse to one MKT_GCS entry.
# Nikita Sinha shows up as "Nikita S", "Nikita S GC" and "Nikita Sinha".
GC_ALIASES = {"Nikita S": "Nikita S GC", "Nikita Sinha": "Nikita S GC"}

# Mirrors of the dashboard's REV_GCS / SCA_GCS, needed so the tasks dump keeps rows
# for the revival and scaling teams too. (Scaling is delisted from the UI but its
# backend is deliberately kept — see SHOW_SCALING in index.html.)
REV_GCS = ["Aanchal Agrawal", "Vishal Thapa", "Debasish Das", "Kavita Rai"]
SCA_GCS = ["Sankhajit Ghosh"]
# Spelling variants / safe shorter prefixes that a first-name split won't produce.
TASK_TOKEN_EXTRAS = ["debashish", "sargun"]


def _task_tokens():
    """Lowercased match tokens for card 10181's assignee_name, from the rosters.

    reduce_rows() substring-matches these against the task owner, so a first name
    is enough ("Nikita S GC" -> "nikita"). Anyone NOT listed here has their tasks
    trimmed out of the snapshot entirely, which is why this must stay in step with
    MKT_GCS / REV_GCS / SCA_GCS rather than being maintained by hand.
    """
    toks = set(TASK_TOKEN_EXTRAS)
    for name in list(MKT_GCS) + list(REV_GCS) + list(SCA_GCS):
        first = str(name).split()[0].strip().lower()
        if first:
            toks.add(first)
    return sorted(toks)


TASK_GC_TOKENS = _task_tokens()


def _canon(s):
    return " ".join(str(s or "").split())


def _gc_canon(s):
    c = _canon(s)
    return GC_ALIASES.get(c, c)


# Card 11011 defines its week columns off CURRENT_DATE() (UTC in BigQuery):
#   w1 = ISO week of (today - 1 week), w2 = -2 weeks, w3 = -3 weeks
# so the columns ROLL FORWARD every Monday 00:00 UTC. We stamp the actual ISO
# week identity of each column into the snapshot; the dashboard labels its
# columns with these and warns when they no longer match the current week
# (i.e. the snapshot predates a Monday rollover) instead of silently showing
# last week's numbers as "latest".
def _iso_week_meta(weeks_ago):
    d = datetime.datetime.utcnow().date() - datetime.timedelta(weeks=weeks_ago)
    y, w, _ = d.isocalendar()
    monday = d - datetime.timedelta(days=d.isoweekday() - 1)
    return {"key": f"{y}{w:02d}",
            "label": f"W{w} · {monday.strftime('%-d %b')}",
            "start": monday.isoformat()}


def build_marketing_sellers(url, H):
    """Marketing Seller View feed. Scope moved OFF the org-locked Google Inputs
    sheet onto Metabase so it stays live + handsfree:
      universe   = card 11011 (Best P&L Visibility - Hits: last 3 weeks spend+pnl)
      GC mapping = card 7753  (seller_manager_mapping: growth_consultant_name etc.)
    A seller is in scope if its 7753 GC (canonicalized) is one of MKT_GCS. The
    old sheet filter keyed on growth_manager=='Pawan Kumar', but in 7753 the GM of
    these GCs is 'Aaruni Vaidya' (and Sargunpreet's is 'Aakash A') — so scope is
    defined by GC membership, NOT GM. Writes committed mb/marketingSellers.json."""
    def pull(cid):
        for attempt in range(4):
            try:
                return req(f"{url}/api/card/{cid}/query/json", 'POST', {}, H)
            except Exception as e:
                last = e
                time.sleep(2 + attempt * 3)
        raise last
    try:
        pnl_rows = pull(11011)   # last-3-week spend + pnl (the active universe)
        map_rows = pull(7753)    # GC / GM / KAE mapping
    except Exception as e:
        print(f"[marketingSellers] FAILED: {e} (keeping previous file)")
        return
    # Daily spend (card 2787: one row per seller, today/yesterday pre-summed).
    try:
        day_rows = pull(2787)
    except Exception as e:
        print(f"[marketingSellers] 2787 daily-spend pull failed ({e}); today/yesterday = null")
        day_rows = []
    day_by = {}
    for r in day_rows:
        sid = str(r.get('seller_id') or '').strip()
        if sid:
            day_by[sid] = {'today': _spend_num(r.get('today_spend')),
                           'yesterday': _spend_num(r.get('yesterday_spend'))}
    mp = {}
    for r in map_rows:
        sid = str(r.get('seller_id') or '').strip()
        if not sid:
            continue
        mp[sid] = {'gc': _gc_canon(r.get('growth_consultant_name')),
                   'gm': _canon(r.get('growth_manager_name')),
                   'kae': _canon(r.get('key_account_executive_name'))}

    def _pnl(v):
        try:
            return round(float(v), 2) if v is not None else None
        except Exception:
            return None

    mkt, sellers, per_gc = set(MKT_GCS), [], {}
    for r in pnl_rows:
        sid = str(r.get('seller_id') or '').strip()
        if not sid:
            continue
        m = mp.get(sid)
        if not m or m['gc'] not in mkt:
            continue
        gc = m['gc']
        w1s = _spend_num(r.get('w1_spend'))
        w2s = _spend_num(r.get('w2_spend'))
        w3s = _spend_num(r.get('w3_spend'))
        dd = day_by.get(sid)
        sellers.append({
            'seller_id': sid,
            'seller_name': _canon(r.get('company')),
            'crm_gc': gc, 'gc_display': gc,
            'crm_gm': '' if m['gm'] in ('-', '') else m['gm'],
            'crm_kae': '' if m['kae'] in ('-', '') else m['kae'],
            # 11011 weekly: latest week (w1) maps to the dashboard's "_w20"
            # (current week) slot, w2->_w19, w3->_w18.
            'spend_w20': round(w1s, 2), 'spend_w19': round(w2s, 2), 'spend_w18': round(w3s, 2),
            'pnl_w20': _pnl(r.get('w1_pnl')), 'pnl_w19': _pnl(r.get('w2_pnl')), 'pnl_w18': _pnl(r.get('w3_pnl')),
            # Daily spend from card 2787 (null if the seller has no 2787 row).
            'today_spend': round(dd['today'], 2) if dd else None,
            'yesterday_spend': round(dd['yesterday'], 2) if dd else None,
            'last_spend_date': None, 'last_spend_date_iso': None,
            'days_since_spend': None, 'is_active_45d': True,
            'website_url': '', 'is_live_w1': w1s > 1,
        })
        per_gc[gc] = per_gc.get(gc, 0) + 1
    wk = [_iso_week_meta(1), _iso_week_meta(2), _iso_week_meta(3)]
    out = {'generatedAt': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           # Real ISO week identity of the w20/w19/w18 columns (see _iso_week_meta).
           'weekKeys': [w['key'] for w in wk],
           'weekLabels': [w['label'] for w in wk],
           'weekStarts': [w['start'] for w in wk],
           'sellers': sellers, 'perGC': per_gc, 'total': len(sellers)}
    path = os.path.join(REPO, "mb", "marketingSellers.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(',', ':'))
    print(f"[marketingSellers] {len(sellers)} sellers across {len(per_gc)} GCs → {path}  perGC={per_gc}")
    return out


# ---- Priority Calling: rank unassigned sellers for revival outreach ----
PRIORITY_SPEND_MIN = 2000   # a week "counts" for PNL tiers when marketing spend > this


def _pweek(year, num):
    try:
        return f"{int(year)}{int(num):02d}"
    except Exception:
        return None


def build_priority_calling(url, H, revival_bySeller):
    """Rank every unassigned seller (universe = revivalSpend/9532) for revival
    outreach, combining 5 signals into a tiered order:
      Tier 1: best weekly PNL% > 0   with marketing spend > 2000  (was profitable)
      Tier 2: best weekly PNL% > -20 with marketing spend > 2000  (near-profitable)
      Tier 3: everyone else
    PNL/spend/spend-gmv are taken as the BEST across three cards per week —
    1880 (blended), 7240 (Facebook), 6911 (Google). Within a tier, sellers are
    ordered by a composite of: recency of pause (10065 last spend, more recent =
    higher), lowest spend/GMV (lower = higher), and ICP (8879, higher = higher).
    Writes mb/priorityCalling.json. Best-effort on quota/errors."""
    if not revival_bySeller:
        print("[priorityCalling] no revival universe — skipped")
        return
    def pull(cid):
        for attempt in range(4):
            try:
                return req(f"{url}/api/card/{cid}/query/json", 'POST', {}, H)
            except Exception as e:
                last = e
                time.sleep(2 + attempt * 3)
        raise last
    try:
        gcv3 = pull(1880)   # blended weekly
        fb   = pull(7240)   # Facebook weekly
        goog = pull(6911)   # Google weekly
        icp  = pull(8879)   # latest ICP score
    except Exception as e:
        print(f"[priorityCalling] FAILED: {e} (keeping previous file)")
        return

    universe = set(revival_bySeller.keys())
    # Per seller: list of (pnl%, marketingSpend, spendGmv) weekly entries across cards.
    entries = {}   # sid -> list of dict(pnl, spend, sgmv)
    def add(sid, pnl, spend, sgmv):
        if sid not in universe:
            return
        entries.setdefault(sid, []).append({'pnl': pnl, 'spend': spend, 'sgmv': sgmv})
    for r in gcv3:
        add(str(r.get('seller_id') or '').strip(),
            _spend_num(r.get('profit/loss_%2')), _spend_num(r.get('marketing_spend_tax_')), _spend_num(r.get('fb_spend/gmv')))
    for r in fb:
        add(str(r.get('seller_id') or '').strip(),
            _spend_num(r.get('net_profit_percentage')), _spend_num(r.get('total_marketing_spend')), _spend_num(r.get('spend_by_gmv')))
    for r in goog:
        add(str(r.get('seller_id') or '').strip(),
            _spend_num(r.get('net_profit_percentage')), _spend_num(r.get('total_marketing_spend')), _spend_num(r.get('spend_by_gmv')))
    icp_by = {}
    for r in icp:
        sid = str(r.get('seller_id') or '').strip()
        if not sid:
            continue
        raw = r.get('icp_score')
        # blank/None ICP → None (seller discarded); a real 0 stays 0.
        icp_by[sid] = _spend_num(raw) if (raw is not None and str(raw).strip() != '') else None

    today = datetime.date.today()
    def days_since(d):
        try:
            return (today - datetime.date.fromisoformat(d[:10])).days
        except Exception:
            return None

    # ---- Banded scores (each 1-10); RPS = P1*P2*P3*P4*P5. ----
    def p1_pnl(v):        # best card PNL% (weeks with spend > 2000)
        if v is None: return 2
        if v > 5:    return 10
        if v > 0:    return 8
        if v > -5:   return 7
        if v > -20:  return 6
        if v > -50:  return 5
        if v > -100: return 4
        return 2
    def p2_recency(days):  # days since last spend (more recent = higher)
        if days is None: return 2
        if days < 1:  return 10
        if days < 3:  return 8
        if days < 7:  return 7
        if days < 12: return 6
        if days < 18: return 5
        if days < 30: return 4
        if days < 45: return 3
        return 2
    def p3_sgmv(v):        # best (lowest) week spend/GMV
        if v is None: return 3
        if v < 10: return 10
        if v < 15: return 9
        if v < 20: return 8
        if v < 30: return 7
        if v < 40: return 6
        if v < 60: return 5
        if v < 90: return 4
        return 3
    def p4_totalspend(v):  # total spend (lower = higher)
        if v is None: v = 0.0
        if v < 5000:   return 10
        if v < 10000:  return 9
        if v < 20000:  return 8
        if v < 30000:  return 7
        if v < 50000:  return 7
        if v < 75000:  return 6
        if v < 100000: return 5
        return 3
    def p5_icp(v):         # ICP — NULL scores 1 (seller NOT discarded)
        if v is None: return 1
        if v > 50: return 10
        if v > 30: return 8
        if v > 20: return 7
        if v > 12: return 6
        if v > 7:  return 5
        return 3

    rows = []
    for sid in universe:
        icp_v = icp_by.get(sid)    # None → P5 scores 1 (kept, not discarded)
        es = entries.get(sid, [])
        qual = [e['pnl'] for e in es if e['spend'] > PRIORITY_SPEND_MIN]
        best_pnl = max(qual) if qual else None
        sgmvs = [e['sgmv'] for e in es if e['sgmv'] and e['sgmv'] > 0]
        min_sgmv = min(sgmvs) if sgmvs else None
        ds = days_since(revival_bySeller[sid].get('lastSpend', ''))
        total_spend = revival_bySeller[sid].get('totalSpend', 0.0)
        s1, s2, s3, s4, s5 = p1_pnl(best_pnl), p2_recency(ds), p3_sgmv(min_sgmv), p4_totalspend(total_spend), p5_icp(icp_v)
        rows.append({
            'sid': sid, 'gc': revival_bySeller[sid].get('gc', ''),
            'lastSpend': revival_bySeller[sid].get('lastSpend', ''),
            'bestPnl': round(best_pnl, 2) if best_pnl is not None else None,
            'minSgmv': round(min_sgmv, 2) if min_sgmv is not None else None,
            'daysSince': ds, 'totalSpend': round(total_spend, 2),
            'icp': round(icp_v, 2) if icp_v is not None else None,
            's1': s1, 's2': s2, 's3': s3, 's4': s4, 's5': s5,
            'rps': s1 * s2 * s3 * s4 * s5,
        })
    # RPS desc; ties broken by P1 > P2 > P3 > P4 > P5 (all desc).
    rows.sort(key=lambda r: (-r['rps'], -r['s1'], -r['s2'], -r['s3'], -r['s4'], -r['s5']))
    for i, r in enumerate(rows):
        r['rank'] = i + 1
    no_icp = sum(1 for r in rows if r['icp'] is None)
    out = {'generatedAt': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'spendMin': PRIORITY_SPEND_MIN, 'total': len(rows), 'noIcp': no_icp, 'sellers': rows}
    path = os.path.join(REPO, "mb", "priorityCalling.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(',', ':'))
    print(f"[priorityCalling] {len(rows)} sellers scored · {no_icp} with no ICP (P5=1) → {path}")


# ---- Hypercare Analysis: accumulating daily trend history + revived sellers ----
# Cards: 12477 (hypercare universe / assigned)  2787 (today+yesterday spend)
#        7669  (last-7-day google+fb spend)     10773 (day-wise spend + spend_gmv)
#        11911 (revived sellers)                9532  (pre-revival spend, via revivalSpend)
#
# WHY A HISTORY FILE: 2787 and 7669 are point-in-time (no history at all), and
# 10773 is a ROLLING ~31-day window. Nothing upstream can answer "what was
# spend/live 3 months ago", so mb/analysisHistory.json is append-only: every run
# merges today's numbers in and NEVER drops a day that has fallen out of 10773's
# window. That file is the only durable record — treat it as data, not a cache.
HC_TEAM = "__team__"          # pseudo-GC key for the whole-team aggregate
HC_LIVE_MIN = 1.0            # "live" = spend strictly greater than this
HC_3K_MIN = 3540.0           # matches the dashboard's SPEND_THRESHOLD
HC_WINDOW = 7                # trailing days for the 3k / spend-gmv qualifier


def _hc_get(r, idx, *names):
    """Read a /query/json row by column NAME, falling back to POSITION.

    Object key order == card column order (see main()), so positional access is
    the documented contract for these cards; names are tried first so a column
    rename upstream doesn't silently shift everything by one.
    """
    for n in names:
        if n in r:
            return r[n]
    ks = list(r.keys())
    return r[ks[idx]] if idx < len(ks) else None


def _hc_parse_ts(v):
    """11911's timestamp is a display string: 'May 22, 2026, 06:01:23'."""
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%b %d, %Y, %H:%M:%S", "%b %d, %Y, %H:%M",
                "%B %d, %Y, %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s[:19] if "T" in s else s, fmt)
        except Exception:
            pass
    try:                      # last resort: leading ISO date
        return datetime.datetime.strptime(s[:10], "%Y-%m-%d")
    except Exception:
        return None


def _hc_ratio(live, assigned):
    return round(100.0 * live / assigned, 2) if assigned else None


def build_hypercare_analysis(url, H, dm_rows=None, revival_bySeller=None, trend=True):
    """Hypercare Analysis feeds: mb/analysisHistory.json + mb/revivedSellers.json.

    History carries three series, team-wide and per GC, for every day we have:
      live      — sellers with that day's spend > 1, over assigned   (from 10773)
      live3k    — sellers with trailing-7d spend > 3540, over assigned (from 10773)
      spendGmv  — spend-weighted spend/GMV% of those qualifying sellers (10773)
    plus point-in-time captures from the cards the metrics are actually defined on:
      liveAuth   — from card 2787 yesterday_spend > 1
      live3kAuth — from card 7669 (google+fb last 7 days) > 3540
    The *Auth series only exist for days this job ran, so the charts draw the
    10773-derived series (consistent + backfillable) and the KPI tiles show the
    authoritative card numbers. They agree to ~1pp in aggregate but NOT per
    seller — 10773 and 7669 genuinely disagree on individual sellers.
    """
    def pull(cid):
        last = None
        for attempt in range(4):
            try:
                return req(f"{url}/api/card/{cid}/query/json", 'POST', {}, H)
            except Exception as e:
                last = e
                time.sleep(2 + attempt * 3)
        raise last

    try:
        uni_rows = pull(12477)    # hypercare universe → the assigned denominator
        rev_rows = pull(11911) if trend else []   # revived sellers (fund-add events)
    except Exception as e:
        print(f"[hypercareAnalysis] FAILED (universe/revived): {e} (keeping previous files)")
        return
    if trend and dm_rows is None:
        try:
            dm_rows = pull(10773)
        except Exception as e:
            print(f"[hypercareAnalysis] FAILED (10773): {e} (keeping previous files)")
            return
    try:
        day_rows = pull(2787)
    except Exception as e:
        print(f"[hypercareAnalysis] 2787 failed ({e}); liveAuth skipped")
        day_rows = []
    try:
        wk7_rows = pull(7669)
    except Exception as e:
        print(f"[hypercareAnalysis] 7669 failed ({e}); live3kAuth skipped")
        wk7_rows = []

    # ---- universe: seller → GC, and the assigned denominator per group -------
    gc_by = {}
    for r in uni_rows:
        sid = str(_hc_get(r, 0, 'seller_id') or '').strip()
        gc = _gc_canon(_hc_get(r, 9, 'growth_consultant_name'))
        if not sid or not gc or gc == '-':
            continue
        gc_by[sid] = gc
    gcs = sorted(set(gc_by.values()))
    assigned = {HC_TEAM: len(gc_by)}
    for gc in gcs:
        assigned[gc] = sum(1 for g in gc_by.values() if g == gc)
    if not gc_by:
        print("[hypercareAnalysis] universe (12477) resolved 0 sellers — aborting, previous files kept")
        return
    groups = [HC_TEAM] + gcs

    # ---- 10773 daily spend + spend_gmv, restricted to the universe -----------
    # Positional schema (see mb/parsers.mjs processDailyMetricsCSV):
    #   0=seller_id, 1=date, 2=spend, 9=spend_gmv
    # NOTE: keep EVERY seller here, not just the universe. The revived-sellers
    # table spans sellers who have left hypercare (only 208 of 266 are in 12477),
    # and restricting this map made their spend_after read 0 instead of unknown.
    # The metric loops below iterate gc_by, so the extra sellers can't leak in.
    spend = {}      # sid -> {date: spend}   (all sellers)
    gmv = {}        # sid -> {date: gmv}
    all_days = set()
    for r in (dm_rows or []):
        sid = str(_hc_get(r, 0, 'seller_id') or '').strip()
        if not sid:
            continue
        d = str(_hc_get(r, 1, 'date') or '')[:10]
        if len(d) != 10 or d[4] != '-':
            continue
        sp = _spend_num(_hc_get(r, 2, 'spend'))
        # Card 10773 exposes GMV directly (col 8). Use it rather than reconstructing it
        # from the s_gmv ratio: s_gmv is NULL whenever gmv is 0, so a ratio-based
        # reconstruction silently drops every seller who spent and returned nothing —
        # i.e. exactly the worst performers — and reports a far rosier number.
        # (Verified: s_gmv == 100*spend/gmv on the daily grain to 6dp, so the "weekly"
        # note in mb/parsers.mjs is stale.)
        gm = _spend_num(_hc_get(r, 8, 'gmv'))
        spend.setdefault(sid, {})[d] = spend.get(sid, {}).get(d, 0.0) + sp
        gmv.setdefault(sid, {})[d] = gmv.get(sid, {}).get(d, 0.0) + gm
        all_days.add(d)
    days = sorted(all_days)
    if trend and not days:
        print("[hypercareAnalysis] 10773 produced no days for the universe — aborting")
        return

    computed = {}
    for i, d in enumerate(days):
        window = days[max(0, i - (HC_WINDOW - 1)):i + 1]
        partial = len(window) < HC_WINDOW      # not enough lookback for a true 7d sum
        # per-seller day spend + trailing-window spend — universe sellers ONLY,
        # so the team aggregate can never pick up a non-hypercare seller.
        live_ids, qual_ids, spender_ids = set(), set(), set()
        for sid in gc_by:
            byd = spend.get(sid)
            if not byd:
                continue
            if byd.get(d, 0.0) > HC_LIVE_MIN:
                live_ids.add(sid)
            if byd.get(d, 0.0) > 0:
                spender_ids.add(sid)
            if sum(byd.get(w, 0.0) for w in window) > HC_3K_MIN:
                qual_ids.add(sid)
        rec_live, rec_3k, rec_sg, rec_sg3 = {}, {}, {}, {}
        for g in groups:
            def _in(sid, _g=g):
                return True if _g == HC_TEAM else gc_by.get(sid) == _g
            n_live = sum(1 for s in live_ids if _in(s))
            n_3k = sum(1 for s in qual_ids if _in(s))
            rec_live[g] = {'n': n_live, 'assigned': assigned[g], 'pct': _hc_ratio(n_live, assigned[g])}
            rec_3k[g] = {'n': n_3k, 'assigned': assigned[g], 'pct': _hc_ratio(n_3k, assigned[g])}
            # Weighted spend/GMV = Σspend / ΣGMV. Two bases, both over SPENDING sellers
            # only (spend > 0 that day) — a seller who didn't spend would otherwise add
            # organic GMV to the denominator with no spend and flatter the ratio:
            #   spendGmv    → every spending seller in the group   (the reported metric)
            #   spendGmv3k  → spending sellers that also cleared ₹3,540 over 7 days
            # A spender whose GMV is 0 MUST be counted: their spend enters the numerator
            # and nothing enters the denominator, which correctly worsens the ratio.
            # Keep the raw totals, not just the ratio: a Week-on-Week spend/GMV is
            # Σspend / ΣGMV over the week, which cannot be recovered from daily
            # percentages (averaging ratios ≠ ratio of sums).
            def _sgmv(ids):
                ts = tg = 0.0
                n = 0
                for s in ids:
                    if not _in(s):
                        continue
                    sp = spend.get(s, {}).get(d, 0.0)
                    if sp <= 0:
                        continue
                    ts += sp
                    tg += gmv.get(s, {}).get(d, 0.0)
                    n += 1
                return {'pct': round(100.0 * ts / tg, 2) if tg > 0 else None,
                        'spend': round(ts, 2), 'gmv': round(tg, 2), 'sellers': n}
            rec_sg[g] = _sgmv(spender_ids)
            rec_sg3[g] = _sgmv(qual_ids)
        computed[d] = {'live': rec_live, 'live3k': rec_3k,
                       'spendGmv': rec_sg, 'spendGmv3k': rec_sg3,
                       'src': '10773', 'partialWindow': partial}

    # ---- point-in-time captures from the authoritative cards ----------------
    today = datetime.datetime.utcnow().date()
    yday = today - datetime.timedelta(days=1)
    auth_live, auth_3k = None, None
    if day_rows:
        n_by = {g: 0 for g in groups}
        for r in day_rows:
            sid = str(_hc_get(r, 0, 'seller_id') or '').strip()
            if sid not in gc_by:
                continue
            if _spend_num(_hc_get(r, 2, 'yesterday_spend')) > HC_LIVE_MIN:
                n_by[HC_TEAM] += 1
                n_by[gc_by[sid]] = n_by.get(gc_by[sid], 0) + 1
        auth_live = {g: {'n': n_by[g], 'assigned': assigned[g], 'pct': _hc_ratio(n_by[g], assigned[g])}
                     for g in groups}
    if wk7_rows:
        n_by = {g: 0 for g in groups}
        for r in wk7_rows:
            sid = str(_hc_get(r, 0, 'seller_id') or '').strip()
            if sid not in gc_by:
                continue
            tot = _spend_num(_hc_get(r, 2, 'google_spend_last7day')) + \
                  _spend_num(_hc_get(r, 3, 'fb_spend_last7day'))
            if tot > HC_3K_MIN:
                n_by[HC_TEAM] += 1
                n_by[gc_by[sid]] = n_by.get(gc_by[sid], 0) + 1
        auth_3k = {g: {'n': n_by[g], 'assigned': assigned[g], 'pct': _hc_ratio(n_by[g], assigned[g])}
                   for g in groups}

    # ---- merge into the append-only history ---------------------------------
    path = os.path.join(REPO, "mb", "analysisHistory.json")
    hist = {}
    if os.path.exists(path):
        try:
            hist = json.load(open(path))
        except Exception as e:
            print(f"[hypercareAnalysis] existing history unreadable ({e}) — starting fresh")
            hist = {}
    hdays = hist.get('days') or {}
    kept = len(hdays)
    for d, rec in computed.items():
        prev = hdays.get(d) or {}
        prev.update(rec)                 # refresh 10773-derived series (upstream revises)
        prev.setdefault('assigned', assigned)
        hdays[d] = prev
    # *Auth values are point-in-time captures. Last write wins within the same
    # calendar day (a later run sees more-complete late-arriving spend), but no
    # run ever touches a day it isn't currently reporting on.
    if auth_live is not None:
        hdays.setdefault(yday.isoformat(), {})['liveAuth'] = auth_live
    if auth_3k is not None:
        hdays.setdefault(today.isoformat(), {})['live3kAuth'] = auth_3k

    prev_latest = (hist.get('latest') or {})
    out = {
        'generatedAt': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'teamKey': HC_TEAM,
        'gcs': gcs,
        'assigned': assigned,
        'thresholds': {'live': HC_LIVE_MIN, 'spend3k': HC_3K_MIN, 'windowDays': HC_WINDOW},
        # In auth-only mode (no 10773 pull) keep whatever window the last full run
        # recorded — don't blank it out.
        'windowFrom': days[0] if days else hist.get('windowFrom'),
        'windowTo': days[-1] if days else hist.get('windowTo'),
        'trendRefreshed': bool(days),
        'latest': {
            'date': today.isoformat(),
            'yesterday': yday.isoformat(),
            'spendLive': auth_live,      # card 2787 · yesterday_spend > 1
            'spend3kLive': auth_3k,      # card 7669 · google+fb last 7d > 3540
            'spendGmv': computed[days[-1]]['spendGmv'] if days else prev_latest.get('spendGmv'),
            'spendGmv3k': computed[days[-1]]['spendGmv3k'] if days else prev_latest.get('spendGmv3k'),
        },
        'sources': {
            'universe': 'card 12477 (all hypercare sellers · assigned denominator)',
            'trend': 'card 10773 (day-wise spend + spend_gmv) — rolling ~31d window',
            'spendLive': 'card 2787 (yesterday_spend > 1)',
            'spend3kLive': 'card 7669 (google+fb last 7 days > 3540)',
            'spendGmv': 'card 10773 Σspend/Σgmv over sellers who spent that day',
            'spendGmv3k': 'same, restricted to sellers above 3540 over 7 days',
        },
        'days': hdays,
    }
    with open(path, "w") as f:
        json.dump(out, f, separators=(',', ':'))
    mode = "full" if trend else "auth-only (no 10773 pull)"
    print(f"[hypercareAnalysis] {mode} · {len(gc_by)} assigned across {len(gcs)} GCs · "
          f"{len(computed)} days recomputed · {len(hdays)} days total (was {kept}) → {path}")

    if trend:
        build_revived_sellers(rev_rows, gc_by, spend, revival_bySeller, days)
    return out


def build_revived_sellers(rev_rows, gc_by, spend_all, revival_bySeller, days):
    """mb/revivedSellers.json — every seller in card 11911 with revival context.

    spendAfter is summed from card 10773, which only holds a rolling ~31 days, so
    for a seller revived before that window it is a PARTIAL figure (flagged with
    windowTruncated). revivalSpend.json's lifetime `spendAfter` (10065 total minus
    9532 pre-revival) is carried alongside as spendAfterLifetime for those cases.
    """
    if revival_bySeller is None:
        try:
            revival_bySeller = json.load(open(os.path.join(REPO, "mb", "revivalSpend.json")))['bySeller']
        except Exception:
            revival_bySeller = {}
    win_start = days[0] if days else None
    # Dedupe to the most-recent revival event per seller (matches build_revival_spend).
    by = {}
    for r in rev_rows:
        sid = str(_hc_get(r, 0, 'seller_id') or '').strip()
        if not sid:
            continue
        ts = _hc_parse_ts(_hc_get(r, 4, 'timestamp'))
        cur = by.get(sid)
        if cur is None or (ts and cur['ts'] and ts > cur['ts']) or (ts and not cur['ts']):
            by[sid] = {'ts': ts,
                       'name': _canon(_hc_get(r, 1, 'seller_name')),
                       'by': _canon(_hc_get(r, 3, 'submitted_by')),
                       'funds': _spend_num(_hc_get(r, 2, 'funds_added_amount_in_rupees')),
                       'n': (cur['n'] + 1) if cur else 1}
        else:
            by[sid]['n'] = by[sid].get('n', 1) + 1

    rows, truncated = [], 0
    for sid, d in by.items():
        rv = revival_bySeller.get(sid) or {}
        rdate = d['ts'].date().isoformat() if d['ts'] else None
        byd = spend_all.get(sid) or {}
        after = None
        last_spend = None
        # No 10773 rows at all → spend_after is UNKNOWN, not zero. Conflating the
        # two made sellers with real post-revival spend look like they never spent.
        if rdate and byd:
            after = round(sum(v for k, v in byd.items() if k >= rdate), 2)
        if byd:
            spent_days = sorted(k for k, v in byd.items() if v > 0)
            last_spend = spent_days[-1] if spent_days else None
        trunc = bool(rdate and win_start and rdate < win_start)
        if trunc:
            truncated += 1
        rows.append({
            'seller_id': sid,
            'seller_name': d['name'] or '',
            'revived_by': d['by'] or '',
            'revived_at': rdate,
            'revival_events': d.get('n', 1),
            'funds_added': d['funds'],
            'current_gc': gc_by.get(sid) or _gc_canon(rv.get('gc')) or '',
            'in_hypercare': sid in gc_by,
            # From card 10773 (windowed) — partial when revived before windowFrom.
            'spend_after': after,
            'window_truncated': trunc,
            # Lifetime equivalent from revivalSpend (10065 total − 9532 pre).
            'spend_after_lifetime': rv.get('spendAfter'),
            'last_spend_date': last_spend or (rv.get('lastSpend') or None),
            # Card 9532 'spend' column = spend BEFORE revival.
            'spend_before_revival': rv.get('preRevival'),
            'decision': rv.get('decision') or '',
        })
    rows.sort(key=lambda r: (r['revived_at'] or ''), reverse=True)
    out = {'generatedAt': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'windowFrom': win_start, 'total': len(rows),
           'windowTruncated': truncated,
           'noPreRevival': sum(1 for r in rows if r['spend_before_revival'] is None),
           'sellers': rows}
    path = os.path.join(REPO, "mb", "revivedSellers.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(',', ':'))
    print(f"[revivedSellers] {len(rows)} revived sellers · {truncated} revived before the "
          f"10773 window (spend_after partial) · {out['noPreRevival']} without a 9532 pre-revival row → {path}")
    return out


def main():
    url, email, pw = creds()
    tok = req(url + "/api/session", 'POST',
              {"username": email, "password": pw},
              {'Content-Type': 'application/json'})['id']
    H = {'Content-Type': 'application/json', 'X-Metabase-Session': tok}

    # Marketing-only mode: refresh just mb/marketingSellers.json (cards 11011 +
    # 7753 + 2787). Cheap enough to run several times a day so the Monday ISO-week
    # rollover of card 11011 is picked up within a couple of hours instead of
    # waiting for the once-daily full pull.
    if '--marketing-only' in sys.argv:
        os.makedirs(OUTDIR, exist_ok=True)
        build_marketing_sellers(url, H)
        return

    # Analysis-only mode: refresh just the Hypercare Analysis feeds
    # (mb/analysisHistory.json + mb/revivedSellers.json). Pulls 12477 + 11911 +
    # 10773 + 2787 + 7669. Runs daily so the trend history keeps accumulating
    # past 10773's rolling ~31-day window.
    if '--analysis-only' in sys.argv or '--analysis-auth-only' in sys.argv:
        os.makedirs(OUTDIR, exist_ok=True)
        # --analysis-auth-only skips the 10773 pull and records ONLY the perishable
        # point-in-time captures (2787 + 7669). The 10773-derived trend can always
        # be re-backfilled for ~31 days, but a missed liveAuth/live3kAuth day is
        # gone forever — so this cheap mode is the daily safety net when the full
        # refresh fails (e.g. build.mjs OOM aborts it before the commit).
        build_hypercare_analysis(url, H, trend='--analysis-auth-only' not in sys.argv)
        return

    os.makedirs(OUTDIR, exist_ok=True)
    manifest = {
        "generatedAt": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "sources": {},
    }
    total_bytes = 0
    failures = []
    dm_rows_for_analysis = None
    for key, cid in CARDS.items():
        # /query/json → ALL rows as objects with RAW typed values (ISO dates,
        # raw numerics). The regular /query endpoint caps at 2000 rows; /query/json
        # does not. Object key order == card column order == the parser's expected
        # positional layout, so we emit cells in first-row key order.
        rows_obj = None
        last_err = None
        for attempt in range(4):
            try:
                rows_obj = req(f"{url}/api/card/{cid}/query/json", 'POST', {}, H)
                break
            except Exception as e:
                last_err = e
                time.sleep(2 + attempt * 3)   # backoff for intermittent 400/5xx under load
        if rows_obj is None:
            print(f"[{key:13s}] card {cid:6d} · FAILED: {last_err} (keeping previous file if any)")
            failures.append(key)
            continue
        raw_n = len(rows_obj)
        rows_obj = reduce_rows(key, rows_obj)
        # Hand 10773 to the Analysis builder instead of pulling it twice — it is
        # the same card and BigQuery has a daily scan quota.
        if key == "dailyMetrics":
            dm_rows_for_analysis = rows_obj
        cols = list(rows_obj[0].keys()) if rows_obj else []
        rows = [[r.get(c) for c in cols] for r in rows_obj]
        csv = rows_to_csv(cols, rows)
        path = os.path.join(OUTDIR, f"{key}.csv")
        with open(path, "w") as f:
            f.write(csv)
        b = os.path.getsize(path)
        total_bytes += b
        manifest["sources"][key] = {"card": cid, "rows": len(rows), "rawRows": raw_n, "bytes": b}
        print(f"[{key:13s}] card {cid:6d} · {raw_n:>7,} → {len(rows):>7,} rows · {b/1e6:6.2f} MB")
    if failures:
        print(f"[warn] {len(failures)} card(s) failed: {failures}")

    # Revival Compliance feed (cards 9532 + 10065 → committed mb/revivalSpend.json)
    revival_bySeller = build_revival_spend(url, H)
    # Priority Calling feed (cards 1880 + 7240 + 6911 + 8879 → mb/priorityCalling.json)
    try:
        build_priority_calling(url, H, revival_bySeller)
    except Exception as e:
        print(f"[priorityCalling] FAILED: {e}")
    # Marketing Seller View feed (cards 11011 + 7753 → mb/marketingSellers.json)
    try:
        build_marketing_sellers(url, H)
    except Exception as e:
        print(f"[marketingSellers] FAILED: {e}")
    # Hypercare Analysis feeds (12477 + 11911 + 2787 + 7669 + the 10773 rows
    # already pulled above → mb/analysisHistory.json + mb/revivedSellers.json)
    try:
        build_hypercare_analysis(url, H, dm_rows=dm_rows_for_analysis,
                                 revival_bySeller=revival_bySeller)
    except Exception as e:
        print(f"[hypercareAnalysis] FAILED: {e}")

    with open(os.path.join(OUTDIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[total] {total_bytes/1e6:.2f} MB across {len(CARDS)} sources → {OUTDIR}")

    if '--push' in sys.argv:
        subprocess.run(['git', '-C', REPO, 'add', 'mb'], check=True)
        r = subprocess.run(['git', '-C', REPO, 'commit', '-m',
                            'Refresh Metabase snapshot (' + manifest["generatedAt"] + ')'],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode == 0:
            subprocess.run(['git', '-C', REPO, 'push', 'origin', 'main'], check=True)
            print("[push] deployed")


if __name__ == '__main__':
    main()
