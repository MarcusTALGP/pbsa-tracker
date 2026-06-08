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

PBSA_HIGH_TYPES   = {"boarding house","co-living","co-living housing","hostel"}
PBSA_MEDIUM_TYPES = {"serviced apartment"}
PBSA_LARGE_TYPES  = {"residential flat building","mixed use development","shop top housing","erection of a new structure"}
PBSA_KEYWORDS     = ["student","pbsa","co-living","hostel","serviced apartment","boarding"]
MIN_COST=5_000_000; MIN_ST=5; MIN_DW=20
STALL_INFO=30; STALL_ASSESS=90; LONG_EXHIBIT=60; NO_UPD=60

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
            if any(t in PBSA_HIGH_TYPES|PBSA_MEDIUM_TYPES|PBSA_LARGE_TYPES for t in types) or any(k in ts for k in PBSA_KEYWORDS):
                found.append(app)
        if page>=data.get("TotalPages",1): break
        page+=1; time.sleep(0.4)
    log.info(f"    PBSA candidates: {len(found)}")
    return found

def score_pbsa(app):
    types=[dt.get("DevelopmentType","").lower() for dt in app.get("DevelopmentType",[])]
    ts=" ".join(types)
    cost=float(app.get("CostOfDevelopment") or 0)
    st=int(app.get("NumberOfStoreys") or 0)
    dw=int(app.get("NumberOfNewDwellings") or 0)
    large = cost>=MIN_COST or st>=MIN_ST or dw>=MIN_DW
    for t in types:
        if t in PBSA_HIGH_TYPES:
            if st>0 and st<3 and dw>0 and dw<10: return "LOW",f"small {t}"
            return ("HIGH" if large else "MEDIUM"), f"dev type: {t}"
    for t in types:
        if t in PBSA_MEDIUM_TYPES: return "MEDIUM",f"dev type: {t}"
    for k in PBSA_KEYWORDS:
        if k in ts: return "HIGH",f"keyword: {k}"
    if any(t in PBSA_LARGE_TYPES for t in types) and large:
        return "LOW",f"large residential (${cost:,.0f}, {st}st, {dw}dw)"
    return "LOW","matched type, small scale"

def pdate(v):
    if not v: return None
    v=str(v).split("T")[0]
    for f in ("%Y-%m-%d","%d/%m/%Y"):
        try: return datetime.strptime(v,f).date().isoformat()
        except: pass
    return None

def map_rec(app, conf, reason):
    loc=(app.get("Location") or [{}])[0]
    lot=(loc.get("Lot") or [{}])[0]
    council=app.get("Council",{})
    lodge=pdate(app.get("LodgementDate"))
    days=None
    if lodge:
        try: days=(date.today()-date.fromisoformat(lodge)).days
        except: pass
    dev_types=", ".join(dt.get("DevelopmentType","") for dt in app.get("DevelopmentType",[]))
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
        "pbsa_match_reason":         reason,
        "enriched_at":               datetime.utcnow().isoformat(),
        "last_updated_at":           datetime.utcnow().isoformat(),
        "last_api_seen_at":          datetime.utcnow().isoformat(),
        "days_in_current_status":    days,
        "alert_flags":               [],
    }

def flags(rec, existing=None):
    f=[]; s=rec.get("application_status",""); d=rec.get("days_in_current_status") or 0
    if s=="Additional Information Requested" and d>=STALL_INFO: f.append("STALLED_INFO")
    if s=="Under Assessment" and d>=STALL_ASSESS: f.append("STALLED_ASSESSMENT")
    if s=="On Exhibition" and d>=LONG_EXHIBIT: f.append("LONG_EXHIBITING")
    if s in ("Rejected","Refused","Declined"): f.append("REJECTED")
    if s=="Withdrawn": f.append("WITHDRAWN")
    if s=="Pending Court Appeal": f.append("COURT_APPEAL")
    if s=="Deferred Commencement": f.append("DEFERRED")
    if existing and s not in ("Determined","Rejected","Withdrawn","Approved"):
        ls=existing.get("last_api_seen_at")
        if ls:
            try:
                ld=datetime.fromisoformat(ls.replace("Z","+00:00")).date()
                if (date.today()-ld).days>=NO_UPD: f.append("NO_UPDATE")
            except: pass
    return f

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
        c,r=score_pbsa(app)
        rec=map_rec(app,c,r)
        if rec["planning_portal_number"]: records.append(rec)

    hi=sum(1 for r in records if r["pbsa_confidence"]=="HIGH")
    me=sum(1 for r in records if r["pbsa_confidence"]=="MEDIUM")
    lo=sum(1 for r in records if r["pbsa_confidence"]=="LOW")
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

    for rec in records:
        rec["alert_flags"]=flags(rec,existing.get(rec["planning_portal_number"]))

    total=0
    for i in range(0,len(records),200):
        sb_upsert("development_applications",records[i:i+200],"planning_portal_number")
        total+=len(records[i:i+200]); log.info(f"Upserted {total}/{len(records)}")

    fl=[r for r in records if r["alert_flags"]]
    log.info(f"=== Done: {len(records)} DAs | {len(history)} changes | {len(fl)} alerts ===")
    for r in fl[:10]:
        log.info(f"  {r['planning_portal_number']} | {r['application_status']} | {r['alert_flags']} | {r['full_address']}")

if __name__=="__main__": main()
