#!/usr/bin/env python3
"""
NSW Planning Portal PBSA DA Fetcher
Uses the DAApplicationTracker API (the real public endpoint).
Fetches DAs in monthly chunks, filters for PBSA types, upserts to Supabase.
"""

import os
import time
import logging
import requests
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

API_URL      = "https://api.apps1.nsw.gov.au/eplanning/data/v0/DAApplicationTracker"
PAGE_SIZE    = 500
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# PBSA development types to capture (matches TYPE_OF_DEVELOPMENT field)
PBSA_HIGH_TYPES = {
    "boarding house",
    "co-living",
    "co-living housing",
    "hostel",
}

PBSA_MEDIUM_TYPES = {
    "serviced apartment",
    "build-to-rent",
}

# Additional keyword search in dev type for student/PBSA references
PBSA_KEYWORDS = [
    "student",
    "pbsa",
    "iglu",
    "scape",
    "unilodge",
    "urbanest",
    "uts:",
    "usyd",
    "unsw",
]

# Alert thresholds (days)
STALL_INFO_DAYS       = 30
STALL_ASSESSMENT_DAYS = 90
LONG_EXHIBITING_DAYS  = 60
NO_UPDATE_DAYS        = 60

# ── Supabase helpers ──────────────────────────────────────────────────────────

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def sb_get(table, params=None):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(),
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def sb_upsert(table, records, on_conflict):
    if not records:
        return []
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        params={"on_conflict": on_conflict},
        json=records,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

def sb_insert(table, records):
    if not records:
        return []
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(),
        json=records,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

# ── NSW Planning Portal API ───────────────────────────────────────────────────

def fetch_month(year, month):
    """Fetch all DAs for a given month, paginating through results."""
    from_date = f"{year}-{month:02d}-01"
    # Last day of month
    first_of_next = date(year, month, 1) + relativedelta(months=1)
    to_date = (first_of_next - timedelta(days=1)).isoformat()

    log.info(f"  Fetching {from_date} → {to_date}")
    
    all_records = []
    page = 1
    
    while True:
        payload = {
            "PageNumber": page,
            "PageSize": PAGE_SIZE,
            "ApplicationStatus": "ALL",
            "LodgementDateFrom": from_date,
            "LodgementDateTo": to_date,
        }
        
        for attempt in range(3):
            try:
                r = requests.post(API_URL, json=payload, timeout=60)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                log.warning(f"    Page {page} attempt {attempt+1} failed: {e}")
                time.sleep(5 * (attempt + 1))
        else:
            log.error(f"    Failed page {page} after 3 attempts, skipping")
            break

        features = data.get("features", [])
        
        # Filter for PBSA on this page
        pbsa = filter_pbsa(features)
        all_records.extend(pbsa)
        
        total_pages = data.get("TotalPages", 1)
        if page >= total_pages:
            break
        
        page += 1
        time.sleep(0.3)
    
    return all_records


def filter_pbsa(features):
    """Filter features for PBSA development types."""
    results = []
    for f in features:
        p = f.get("properties", {})
        dev_types_raw = (p.get("TYPE_OF_DEVELOPMENT") or "").lower()
        dev_types = [t.strip() for t in dev_types_raw.split(",")]
        
        confidence = None
        reason = None
        
        # Check high confidence types
        for dt in dev_types:
            if dt in PBSA_HIGH_TYPES:
                confidence = "HIGH"
                reason = f"dev type: {dt}"
                break
        
        # Check medium confidence types
        if not confidence:
            for dt in dev_types:
                if dt in PBSA_MEDIUM_TYPES:
                    confidence = "MEDIUM"
                    reason = f"dev type: {dt}"
                    break
        
        # Check keyword matches
        if not confidence:
            for kw in PBSA_KEYWORDS:
                if kw in dev_types_raw:
                    confidence = "MEDIUM"
                    reason = f"keyword: {kw}"
                    break
        
        if confidence:
            results.append({
                "raw": p,
                "confidence": confidence,
                "reason": reason,
                "coords": f.get("geometry", {}).get("coordinates", [None, None]),
            })
    
    return results


# ── Record mapping ────────────────────────────────────────────────────────────

def parse_date(val):
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(val), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def map_record(item):
    p = item["raw"]
    coords = item["coords"]
    pan = (p.get("PLANNING_PORTAL_APP_NUMBER") or "").strip()
    status = p.get("STATUS") or ""
    lodgement = parse_date(p.get("LODGEMENT_DATE"))
    
    days_in_status = None
    if lodgement:
        try:
            days_in_status = (date.today() - date.fromisoformat(lodgement)).days
        except Exception:
            pass

    return {
        "planning_portal_number": pan,
        "council_name":           p.get("COUNCIL_NAME"),
        "application_status":     status,
        "application_type":       p.get("APPLICATION_TYPE"),
        "development_type":       p.get("TYPE_OF_DEVELOPMENT"),
        "full_address":           p.get("FULL_ADDRESS"),
        "lodgement_date":         lodgement,
        "determination_date":     parse_date(p.get("DETERMINATION_DATE")),
        "longitude":              coords[0] if coords else None,
        "latitude":               coords[1] if coords else None,
        "pbsa_confidence":        item["confidence"],
        "pbsa_match_reason":      item["reason"],
        "last_updated_at":        datetime.utcnow().isoformat(),
        "last_api_seen_at":       datetime.utcnow().isoformat(),
        "days_in_current_status": days_in_status,
        "alert_flags":            [],
    }


def compute_flags(rec, existing=None):
    flags = []
    status = rec.get("application_status", "")
    days = rec.get("days_in_current_status") or 0

    if status == "Additional Information Requested" and days >= STALL_INFO_DAYS:
        flags.append("STALLED_INFO")
    if status == "Under Assessment" and days >= STALL_ASSESSMENT_DAYS:
        flags.append("STALLED_ASSESSMENT")
    if status == "On Exhibition" and days >= LONG_EXHIBITING_DAYS:
        flags.append("LONG_EXHIBITING")
    if status in ("Rejected", "Refused", "Declined"):
        flags.append("REJECTED")
    if status == "Withdrawn":
        flags.append("WITHDRAWN")
    if status == "Pending Court Appeal":
        flags.append("COURT_APPEAL")
    if status == "Deferred Commencement":
        flags.append("DEFERRED")

    if existing and status not in ("Determined", "Rejected", "Withdrawn", "Approved"):
        last_seen = existing.get("last_api_seen_at")
        if last_seen:
            try:
                last_date = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).date()
                if (date.today() - last_date).days >= NO_UPDATE_DAYS:
                    flags.append("NO_UPDATE")
            except Exception:
                pass

    return flags


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== PBSA DA Fetcher starting ===")

    full_sync = os.environ.get("FULL_SYNC", "false").lower() == "true"
    days_back = int(os.environ.get("DAYS_BACK", "14"))

    if full_sync:
        start_date = date(2019, 1, 1)
        log.info("FULL SYNC: fetching from 2019-01-01")
    else:
        start_date = date.today() - timedelta(days=days_back)
        log.info(f"INCREMENTAL: fetching from {start_date}")

    # Build list of (year, month) tuples to fetch
    months = []
    current = date(start_date.year, start_date.month, 1)
    today = date.today()
    while current <= today:
        months.append((current.year, current.month))
        current += relativedelta(months=1)

    log.info(f"Fetching {len(months)} month(s)")

    all_pbsa = []
    for year, month in months:
        items = fetch_month(year, month)
        log.info(f"  {year}-{month:02d}: {len(items)} PBSA DAs found")
        all_pbsa.extend(items)

    log.info(f"Total PBSA DAs found: {len(all_pbsa)}")
    if not all_pbsa:
        log.info("Nothing to upsert.")
        return

    # Deduplicate by PAN (keep last occurrence)
    seen = {}
    for item in all_pbsa:
        pan = (item["raw"].get("PLANNING_PORTAL_APP_NUMBER") or "").strip()
        if pan:
            seen[pan] = item
    all_pbsa = list(seen.values())
    log.info(f"After dedup: {len(all_pbsa)} unique DAs")

    # Map to DB records
    records = [map_record(item) for item in all_pbsa]
    records = [r for r in records if r["planning_portal_number"]]

    # Fetch existing records for status change detection
    pans = [r["planning_portal_number"] for r in records]
    existing = {}
    chunk_size = 100
    for i in range(0, len(pans), chunk_size):
        chunk = pans[i:i+chunk_size]
        in_clause = "(" + ",".join(f'"{p}"' for p in chunk) + ")"
        rows = sb_get("development_applications", {
            "planning_portal_number": f"in.{in_clause}",
            "select": "planning_portal_number,application_status,last_api_seen_at",
        })
        for row in rows:
            existing[row["planning_portal_number"]] = row

    # Detect status changes
    history = []
    for rec in records:
        pan = rec["planning_portal_number"]
        old = existing.get(pan)
        if old and old["application_status"] != rec["application_status"]:
            history.append({
                "pan": pan,
                "old_status": old["application_status"],
                "new_status": rec["application_status"],
            })
            log.info(f"Status change: {pan} {old['application_status']} → {rec['application_status']}")

    if history:
        sb_insert("status_history", history)
        log.info(f"Recorded {len(history)} status changes")

    # Compute alert flags
    for rec in records:
        rec["alert_flags"] = compute_flags(rec, existing.get(rec["planning_portal_number"]))

    # Upsert in chunks
    total = 0
    for i in range(0, len(records), 200):
        chunk = records[i:i+200]
        sb_upsert("development_applications", chunk, "planning_portal_number")
        total += len(chunk)
        log.info(f"Upserted {total}/{len(records)}")

    # Summary
    flagged = [r for r in records if r["alert_flags"]]
    log.info(f"=== Done: {len(records)} DAs upserted, {len(history)} status changes, {len(flagged)} with alerts ===")
    for r in flagged[:10]:
        log.info(f"  {r['planning_portal_number']} | {r['application_status']} | {r['alert_flags']} | {r['full_address']}")


if __name__ == "__main__":
    main()
