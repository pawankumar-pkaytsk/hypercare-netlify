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
TASK_GC_TOKENS = [
    "aaruni", "sadiya", "nikita", "nishan", "dev vashisth", "vashisth",
    "sejal", "aanchal", "vishal", "debasish", "debashish", "sankhajit",
]
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
MKT_GCS = ["Aaruni Vaidya", "Sadiya Rajgoli", "Nikita S GC", "Dev Vashisth",
           "Tanaya Gore", "Sargunpreet Singh"]


def _canon(s):
    return " ".join(str(s or "").split())


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
    mp = {}
    for r in map_rows:
        sid = str(r.get('seller_id') or '').strip()
        if not sid:
            continue
        mp[sid] = {'gc': _canon(r.get('growth_consultant_name')),
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
        sellers.append({
            'seller_id': sid,
            'seller_name': _canon(r.get('company')),
            'crm_gc': gc, 'gc_display': gc,
            'crm_gm': '' if m['gm'] in ('-', '') else m['gm'],
            'crm_kae': '' if m['kae'] in ('-', '') else m['kae'],
            # 11011 has no daily data; latest week (w1) maps to the dashboard's
            # "_w20" (current week) slot, w2->_w19, w3->_w18.
            'spend_w20': round(w1s, 2), 'spend_w19': round(w2s, 2), 'spend_w18': round(w3s, 2),
            'pnl_w20': _pnl(r.get('w1_pnl')), 'pnl_w19': _pnl(r.get('w2_pnl')), 'pnl_w18': _pnl(r.get('w3_pnl')),
            'today_spend': None, 'yesterday_spend': None,
            'last_spend_date': None, 'last_spend_date_iso': None,
            'days_since_spend': None, 'is_active_45d': True,
            'website_url': '', 'is_live_w1': w1s > 1,
        })
        per_gc[gc] = per_gc.get(gc, 0) + 1
    out = {'generatedAt': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'weekLabels': ['Latest wk', 'Prev wk', '2 wks ago'],
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


def main():
    url, email, pw = creds()
    tok = req(url + "/api/session", 'POST',
              {"username": email, "password": pw},
              {'Content-Type': 'application/json'})['id']
    H = {'Content-Type': 'application/json', 'X-Metabase-Session': tok}

    os.makedirs(OUTDIR, exist_ok=True)
    manifest = {
        "generatedAt": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "sources": {},
    }
    total_bytes = 0
    failures = []
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
