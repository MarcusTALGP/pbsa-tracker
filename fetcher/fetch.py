#!/usr/bin/env python3
"""
NSW Planning Portal PBSA DA Fetcher
Uses the OnlineDA Open Data API — fully public, no key required.
Filters are passed as HTTP HEADERS (not query params).
"""

import os, re, json, time, logging, requests
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ONLINE_DA_URL = "https://api.apps1.nsw.gov.au/eplanning/data/v0/OnlineDA"
PAGE_SIZE     = 100
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]

# ── Asset class classification ────────────────────────────────────────────────
# Each DA gets tagged with an asset_class: PBSA | CO_LIVING | BTR | ADJACENT
# Strongest signal wins. Confidence (HIGH/MEDIUM/LOW) is scored within each class.

PBSA_KEYWORDS = ["student accommodation","student housing","student residence","pbsa",
                 "purpose built student","purpose-built student","university accommodation",
                 "iglu","scape","unilodge","urbanest","student living"]

COLIVING_TYPES    = {"co-living","co-living housing"}
COLIVING_KEYWORDS = ["co-living","coliving","co living"]

BTR_KEYWORDS = ["build to rent","build-to-rent"," btr ","build to rent housing"]

BOARDING_TYPES = {"boarding house","hostel"}

LARGE_RES_TYPES = {"residential flat building","mixed use development",
                   "shop top housing","erection of a new structure"}

ALL_CANDIDATE_TYPES = COLIVING_TYPES | BOARDING_TYPES | LARGE_RES_TYPES | {"serviced apartment"}
ALL_KEYWORDS = PBSA_KEYWORDS + COLIVING_KEYWORDS + BTR_KEYWORDS + ["boarding","hostel","serviced apartment"]

MIN_COST=5_000_000; MIN_ST=5; MIN_DW=20

def sb_h(): return {"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}","Content-Type":"application/json","Prefer":"return=representation"}
def sb_get(t,p=None): r=requests.get(f"{SUPABASE_URL}/rest/v1/{t}",headers=sb_h(),params=p or {},timeout=30); r.raise_for_status(); return r.json()
def sb_upsert(t,recs,oc):
    if not recs: return []
    r=requests.post(f"{SUPABASE_URL}/rest/v1/{t}",headers={**sb_h(),"Prefer":f"resolution=merge-duplicates,return=representation"},params={"on_conflict":oc},json=recs,timeout=60); r.raise_for_status(); return r.json()
def sb_insert(t,recs):
    if not recs: return []
    r=requests.post(f"{SUPABASE_URL}/rest/v1/{t}",headers=sb_h(),json=recs,timeout=30); r.raise_for_status(); return r.json()

def fetch_page(filters, page):
    hdrs={"PageSize":str(PAGE_SIZE),"PageNumber":str(page),"filters":json.dumps({"filters":filters})}
    for attempt in range(3):
        try:
            r=requests.get(ONLINE_DA_URL,headers=hdrs,timeout=60); r.raise_for_status(); return r.json()
        except Exception as e:
            log.warning(f"Page {page} attempt {attempt+1}: {e}"); time.sleep(5*(attempt+1))
    raise RuntimeError(f"Failed page {page}")

def fetch_period(d_from, d_to):
    log.info(f"  {d_from} → {d_to}")
    filters={"LodgementDateFrom":d_from,"LodgementDateTo":d_to,"ApplicationType":"Development Application"}
    found=[]; page=1
    while True:
        data=fetch_page(filters,page)
        apps=data.get("Application",[])
        if page==1: log.info(f"    Total: {data.get('TotalCount',0)} ({data.get('TotalPages',1)} pages)")
        for app in apps:
            types=[dt.get("DevelopmentType","").lower() for dt in app.get("DevelopmentType",[])]
            ts=" ".join(types)
            if any(t in ALL_CANDIDATE_TYPES for t in types) or any(k in ts for k in ALL_KEYWORDS):
                found.append(app)
        if page>=data.get("TotalPages",1): break
        page+=1; time.sleep(0.4)
    log.info(f"    PBSA candidates: {len(found)}")
    return found

def classify(app):
    """
    Returns (asset_class, confidence, reason).
    asset_class: PBSA | CO_LIVING | BTR | ADJACENT
    confidence:  HIGH | MEDIUM | LOW
    Strongest signal wins. Scale (cost/storeys/dwellings) refines confidence.
    """
    types=[dt.get("DevelopmentType","").lower() for dt in app.get("DevelopmentType",[])]
    ts=" ".join(types)
    cost=float(app.get("CostOfDevelopment") or 0)
    st=int(app.get("NumberOfStoreys") or 0)
    dw=int(app.get("NumberOfNewDwellings") or 0)
    large = cost>=MIN_COST or st>=MIN_ST or dw>=MIN_DW
    tiny  = (st>0 and st<3) and (dw>0 and dw<10)

    # 1. PBSA — explicit student keywords are the strongest signal
    for k in PBSA_KEYWORDS:
        if k in ts:
            return "PBSA","HIGH",f"student keyword: {k}"

    # 2. CO-LIVING — explicit co-living type or keyword
    if any(t in COLIVING_TYPES for t in types) or any(k in ts for k in COLIVING_KEYWORDS):
        if tiny: return "CO_LIVING","LOW",f"small co-living ({st}st, {dw}dw)"
        return "CO_LIVING", ("HIGH" if large else "MEDIUM"), "co-living development"

    # 3. BTR — explicit build-to-rent keyword
    if any(k in ts for k in BTR_KEYWORDS):
        return "BTR", ("HIGH" if large else "MEDIUM"), "build-to-rent development"

    # 4. Boarding house / hostel — the classic PBSA type in NSW planning.
    #    These ARE student/managed accommodation. Tag as PBSA, scale sets confidence.
    for t in types:
        if t in BOARDING_TYPES:
            if large:
                return "PBSA","HIGH",f"large {t} (${cost:,.0f}, {st}st, {dw}dw)"
            if tiny:
                return "PBSA","LOW",f"small {t} ({st}st, {dw}dw)"
            return "PBSA","MEDIUM",f"{t}"

    # 5. Serviced apartment — PBSA-adjacent
    if "serviced apartment" in ts:
        return "ADJACENT", ("MEDIUM" if large else "LOW"), "serviced apartment"

    # 6. Large residential pulled in as candidate — flag as ADJACENT for manual review
    if any(t in LARGE_RES_TYPES for t in types) and large:
        return "ADJACENT","LOW",f"large residential — review (${cost:,.0f}, {st}st, {dw}dw)"

    return "ADJACENT","LOW","matched candidate type, small scale"

def pdate(v):
    if not v: return None
    v=str(v).split("T")[0]
    for f in ("%Y-%m-%d","%d/%m/%Y"):
        try: return datetime.strptime(v,f).date().isoformat()
        except: pass
    return None

def map_rec(app, asset_class, conf, reason):
    loc=(app.get("Location") or [{}])[0]
    lot=(loc.get("Lot") or [{}])[0]
    council=app.get("Council",{})
    lodge=pdate(app.get("LodgementDate"))
    days=None
    if lodge:
        try: days=(date.today()-date.fromisoformat(lodge)).days
        except: pass
    dev_types=", ".join(dt.get("DevelopmentType","") for dt in app.get("DevelopmentType",[]))
    # Dormancy: how long since the DA last had any recorded activity
    last_updated = pdate(app.get("DateLastUpdated"))
    dormant_days = None
    if last_updated:
        try: dormant_days=(date.today()-date.fromisoformat(last_updated)).days
        except: pass
    return {
        "planning_portal_number":    app.get("PlanningPortalApplicationNumber","").strip(),
        "council_application_number":app.get("CouncilApplicationNumber"),
        "council_name":              council.get("CouncilName"),
        "application_type":          app.get("ApplicationType"),
        "application_status":        app.get("ApplicationStatus"),
        "development_type":          dev_types,
        "full_address":              loc.get("FullAddress"),
        "suburb":                    loc.get("Suburb"),
        "postcode":                  loc.get("Postcode"),
        "street_name":               loc.get("StreetName"),
        "street_number":             loc.get("StreetNumber1"),
        "lot":                       lot.get("Lot"),
        "plan_label":                lot.get("PlanLabel"),
        "cost_of_development":       app.get("CostOfDevelopment"),
        "number_of_new_dwellings":   app.get("NumberOfNewDwellings"),
        "number_of_storeys":         app.get("NumberOfStoreys"),
        "lodgement_date":            lodge,
        "determination_date":        pdate(app.get("DeterminationDate")),
        "determination_authority":   app.get("DeterminationAuthority"),
        "exhibition_start_date":     pdate(app.get("AssessmentExhibitionStartDate")),
        "exhibition_end_date":       pdate(app.get("AssessmentExhibitionEndDate")),
        "epi_variation_proposed":    app.get("EPIVariationProposedFlag")=="Y",
        "accompanied_by_vpa":        app.get("AccompaniedByVPAFlag")=="Y",
        "vpa_status":                app.get("VPAStatus"),
        "longitude":                 loc.get("X"),
        "latitude":                  loc.get("Y"),
        "pbsa_confidence":           conf,
        "asset_class":               asset_class,
        "pbsa_match_reason":         reason,
        "enriched_at":               datetime.utcnow().isoformat(),
        "last_updated_at":           datetime.utcnow().isoformat(),
        "last_api_seen_at":          datetime.utcnow().isoformat(),
        "days_in_current_status":    days,
        "date_last_updated":         last_updated,
        "dormant_days":              dormant_days,
        "alert_flags":               [],
    }

# No editorial flags. The dashboard filters on raw factual fields:
#   application_status (Rejected / Withdrawn / Under Assessment / etc.)
#   days_in_current_status (days since lodgement)
# You make your own call from the raw data.

def main():
    log.info("=== PBSA Fetcher (OnlineDA API) ===")
    full=os.environ.get("FULL_SYNC","false").lower()=="true"
    days_back=int(os.environ.get("DAYS_BACK","14"))
    start=date(2018,12,10) if full else date.today()-timedelta(days=days_back)
    log.info(f"Mode: {'FULL SYNC from 2018-12-10' if full else f'last {days_back} days from {start}'}")

    months=[]
    cur=date(start.year,start.month,1); today=date.today()
    while cur<=today:
        months.append(cur); cur+=relativedelta(months=1)
    log.info(f"{len(months)} month(s) to fetch")

    raw=[]
    for m in months:
        df=m.isoformat()
        dt=min(m+relativedelta(months=1)-timedelta(days=1),today).isoformat()
        raw.extend(fetch_period(df,dt))

    log.info(f"Raw candidates: {len(raw)}")
    if not raw: return

    seen={}
    for app in raw:
        p=app.get("PlanningPortalApplicationNumber","").strip()
        if p: seen[p]=app
    log.info(f"Unique PANs: {len(seen)}")

    records=[]
    for pan,app in seen.items():
        ac,c,r=classify(app)
        rec=map_rec(app,ac,c,r)
        if rec["planning_portal_number"]: records.append(rec)

    hi=sum(1 for r in records if r["pbsa_confidence"]=="HIGH")
    me=sum(1 for r in records if r["pbsa_confidence"]=="MEDIUM")
    lo=sum(1 for r in records if r["pbsa_confidence"]=="LOW")
    by_class={}
    for r in records: by_class[r["asset_class"]]=by_class.get(r["asset_class"],0)+1
    log.info(f"By class: {by_class}")
    log.info(f"Scored: HIGH={hi} MEDIUM={me} LOW={lo}")

    pans=[r["planning_portal_number"] for r in records]
    existing={}
    for i in range(0,len(pans),100):
        chunk=pans[i:i+100]
        in_c="("+",".join(f'"{p}"' for p in chunk)+")"
        rows=sb_get("development_applications",{"planning_portal_number":f"in.{in_c}","select":"planning_portal_number,application_status,last_api_seen_at"})
        for row in rows: existing[row["planning_portal_number"]]=row

    history=[]
    for rec in records:
        pan=rec["planning_portal_number"]; old=existing.get(pan)
        if old and old["application_status"]!=rec["application_status"]:
            history.append({"pan":pan,"old_status":old["application_status"],"new_status":rec["application_status"]})
            log.info(f"Status change: {pan} {old['application_status']} → {rec['application_status']}")
    if history: sb_insert("status_history",history); log.info(f"{len(history)} status changes")

    total=0
    for i in range(0,len(records),200):
        sb_upsert("development_applications",records[i:i+200],"planning_portal_number")
        total+=len(records[i:i+200]); log.info(f"Upserted {total}/{len(records)}")

    # Summary by status — factual, no editorial labels
    by_status={}
    for r in records: by_status[r["application_status"]]=by_status.get(r["application_status"],0)+1
    log.info(f"=== Done: {len(records)} DAs | {len(history)} status changes ===")
    log.info(f"By status: {by_status}")

if __name__=="__main__": main()
