#!/usr/bin/env python3
"""
NSW Planning Portal PBSA DA Fetcher
Runs daily via GitHub Actions. Fetches all DAs, filters for PBSA,
upserts to Supabase, tracks status changes, and sets alert flags.
"""

import os
import re
import time
import logging
import requests
from datetime import date, datetime, timedelta
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE      = "https://api.apps1.nsw.gov.au/eplanning/data/v0/OnlineDA"
PAGE_SIZE     = 500
SUPABASE_URL  = os.environ["SUPABASE_URL"]       # set in GitHub Actions secrets
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]       # service_role key

# PBSA keyword matching — ordered by confidence
PBSA_HIGH = [
    r"purpose.built student accommodation",
    r"\bPBSA\b",
    r"student accommodation",
    r"student housing",
    r"student residence",
    r"student residential",
]
PBSA_MEDIUM = [
    r"boarding house",
    r"co-living",
    r"coliving",
    r"managed student",
    r"university accommodation",
    r"tertiary accommodation",
    r"student apartment",
]
PBSA_LOW = [
    r"serviced apartment",
    r"micro.apartment",
    r"micro apartment",
    r"build.to.rent",
]

# Alert thresholds (days)
STALL_INFO_DAYS        = 30
STALL_ASSESSMENT_DAYS  = 90
LONG_EXHIBITING_DAYS   = 60
NO_UPDATE_DAYS         = 60
MIN_COST_PBSA_INFER    = 5_000_000   # $5M+ commercial/residential flagged as LOW

# ── Supabase client helpers ───────────────────────────────────────────────────

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def sb_get(table: str, params: dict = None):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(),
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def sb_upsert(table: str, records: list, on_conflict: str):
    if not records:
        return []
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**sb_headers(), "Prefer": f"resolution=merge-duplicates,return=representation"},
        params={"on_conflict": on_conflict},
        json=records,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

def sb_insert(table: str, records: list):
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

def fetch_page(page_number: int, lodgement_from: str, lodgement_to: str) -> dict:
    """Fetch one page of DAs from the NSW Planning Portal open data API."""
    params = {
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
        "filters": (
            f'{{"LodgementDateFrom":"{lodgement_from}",'
            f'"LodgementDateTo":"{lodgement_to}"}}'
        ),
    }
    for attempt in range(3):
        try:
            r = requests.get(API_BASE, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"Page {page_number} attempt {attempt+1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch page {page_number} after 3 attempts")


def fetch_all_das(days_back: int = 7) -> list[dict]:
    """
    Fetch DAs updated in the last N days.
    On first run (FULL_SYNC=true env var), fetches from 2019-01-01.
    """
    full_sync = os.environ.get("FULL_SYNC", "false").lower() == "true"
    if full_sync:
        lodgement_from = "2019-01-01"
        log.info("FULL SYNC mode — fetching from 2019-01-01")
    else:
        lodgement_from = (date.today() - timedelta(days=days_back)).isoformat()

    lodgement_to = date.today().isoformat()
    log.info(f"Fetching DAs lodged {lodgement_from} → {lodgement_to}")

    all_das = []
    page = 1
    while True:
        data = fetch_page(page, lodgement_from, lodgement_to)

        # Handle API response structure variations
        records = data if isinstance(data, list) else data.get("Application", data.get("applications", []))
        if not records:
            break

        all_das.extend(records)
        log.info(f"Page {page}: {len(records)} records (total so far: {len(all_das)})")

        if len(records) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.5)  # be polite

    log.info(f"Total DAs fetched: {len(all_das)}")
    return all_das

# ── PBSA Detection ────────────────────────────────────────────────────────────

def pbsa_confidence(da: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (confidence_level, match_reason) or (None, None) if not PBSA.
    Checks development_type, description, and cost/category inference.
    """
    # Fields to search
    searchable = " ".join(filter(None, [
        da.get("DevelopmentType", ""),
        da.get("DevelopmentDescription", ""),
        da.get("ApplicationDescription", ""),
    ])).lower()

    for pattern in PBSA_HIGH:
        if re.search(pattern, searchable, re.IGNORECASE):
            return "HIGH", f"keyword match: {pattern}"

    for pattern in PBSA_MEDIUM:
        if re.search(pattern, searchable, re.IGNORECASE):
            return "MEDIUM", f"keyword match: {pattern}"

    for pattern in PBSA_LOW:
        if re.search(pattern, searchable, re.IGNORECASE):
            return "LOW", f"keyword match: {pattern}"

    # Inference: large commercial/residential development with no other strong signal
    try:
        cost = float(da.get("CostOfDevelopment", 0) or 0)
        category = da.get("DevelopmentCategory", "").lower()
        dev_type = da.get("DevelopmentType", "").lower()
        if (cost >= MIN_COST_PBSA_INFER
                and category in ("commercial", "residential")
                and any(k in dev_type for k in ("residential flat", "mixed use", "multi dwelling"))):
            return "LOW", f"inferred: ${cost:,.0f} {category} development"
    except (ValueError, TypeError):
        pass

    return None, None

# ── Alert Flag Logic ──────────────────────────────────────────────────────────

def compute_flags(record: dict, existing_record: Optional[dict] = None) -> list[str]:
    """Compute alert flags for a DA record."""
    flags = []
    status = record.get("application_status", "")
    today = date.today()

    # Days in current status
    days = record.get("days_in_current_status", 0) or 0

    if status == "Additional Information Requested" and days >= STALL_INFO_DAYS:
        flags.append("STALLED_INFO")
    if status == "Under Assessment" and days >= STALL_ASSESSMENT_DAYS:
        flags.append("STALLED_ASSESSMENT")
    if status == "On Exhibition" and days >= LONG_EXHIBITING_DAYS:
        flags.append("LONG_EXHIBITING")
    if status == "Rejected":
        flags.append("REJECTED")
    if status == "Withdrawn":
        flags.append("WITHDRAWN")
    if status == "Pending Court Appeal":
        flags.append("COURT_APPEAL")
    if status == "Deferred Commencement":
        flags.append("DEFERRED")

    # No API update recently (but still active)
    if existing_record and status not in ("Determined", "Rejected", "Withdrawn"):
        last_seen = existing_record.get("last_api_seen_at")
        if last_seen:
            try:
                last_seen_date = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).date()
                if (today - last_seen_date).days >= NO_UPDATE_DAYS:
                    flags.append("NO_UPDATE")
            except Exception:
                pass

    return flags

# ── Record Mapping ────────────────────────────────────────────────────────────

def parse_date(val) -> Optional[str]:
    if not val:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(val), fmt).date().isoformat()
        except ValueError:
            continue
    return None

def parse_bool(val) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() in ("Y", "YES", "TRUE", "1")

def map_da(raw: dict, confidence: str, reason: str) -> dict:
    """Map raw API record to our DB schema."""
    lodgement = parse_date(raw.get("LodgementDate"))
    determination = parse_date(raw.get("DeterminationDate"))
    status = raw.get("ApplicationStatus", "")

    # Days in current status (rough estimate from lodgement if no better data)
    days_in_status = None
    try:
        ref_date_str = lodgement
        if ref_date_str:
            ref = date.fromisoformat(ref_date_str)
            days_in_status = (date.today() - ref).days
    except Exception:
        pass

    return {
        "planning_portal_number":    raw.get("PlanningPortalApplicationNumber", "").strip(),
        "council_application_number": raw.get("CouncilApplicationNumber"),
        "council_name":              raw.get("CouncilName"),
        "application_type":          raw.get("ApplicationType"),
        "application_status":        status,
        "development_type":          raw.get("DevelopmentType"),
        "development_category":      raw.get("DevelopmentCategory"),
        "development_description":   raw.get("DevelopmentDescription") or raw.get("ApplicationDescription"),
        "full_address":              raw.get("FullAddress"),
        "suburb":                    raw.get("Suburb"),
        "postcode":                  raw.get("Postcode"),
        "street_name":               raw.get("StreetName"),
        "street_number":             raw.get("StreetNumber1"),
        "lot":                       raw.get("Lot"),
        "plan_label":                raw.get("PlanLabel"),
        "cost_of_development":       raw.get("CostOfDevelopment"),
        "number_of_new_dwellings":   raw.get("NumberOfNewDwellings"),
        "number_of_storeys":         raw.get("NumberOfStoreys"),
        "lodgement_date":            lodgement,
        "determination_date":        determination,
        "determination_authority":   raw.get("DeterminationAuthority"),
        "exhibition_start_date":     parse_date(raw.get("AssessmentExhibitionStartDate")),
        "exhibition_end_date":       parse_date(raw.get("AssessmentExhibitionEndDate")),
        "epi_variation_proposed":    parse_bool(raw.get("EPIVariationProposedFlag")),
        "epi_variation_approved":    parse_bool(raw.get("VariationToDevelopmentStandardsApprovedFlag")),
        "accompanied_by_vpa":        parse_bool(raw.get("AccompaniedByVPAFlag")),
        "vpa_status":                raw.get("VPAStatus"),
        "subdivision_proposed":      parse_bool(raw.get("SubdivisionProposedFlag")),
        "longitude":                 raw.get("X"),
        "latitude":                  raw.get("Y"),
        "pbsa_confidence":           confidence,
        "pbsa_match_reason":         reason,
        "last_updated_at":           datetime.utcnow().isoformat(),
        "last_api_seen_at":          datetime.utcnow().isoformat(),
        "days_in_current_status":    days_in_status,
        "alert_flags":               [],   # computed after upsert
    }

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== PBSA DA Fetcher starting ===")

    # 1. Fetch from API
    raw_das = fetch_all_das(days_back=int(os.environ.get("DAYS_BACK", "14")))

    # 2. Filter for PBSA
    pbsa_records = []
    for raw in raw_das:
        pan = (raw.get("PlanningPortalApplicationNumber") or "").strip()
        if not pan:
            continue
        confidence, reason = pbsa_confidence(raw)
        if not confidence:
            continue
        pbsa_records.append(map_da(raw, confidence, reason))

    log.info(f"PBSA DAs identified: {len(pbsa_records)}")
    if not pbsa_records:
        log.info("Nothing to upsert. Done.")
        return

    # 3. Fetch existing records from Supabase to detect status changes
    pans = [r["planning_portal_number"] for r in pbsa_records]
    # Supabase REST: filter by IN list
    existing = {}
    chunk_size = 100
    for i in range(0, len(pans), chunk_size):
        chunk = pans[i:i+chunk_size]
        in_clause = "(" + ",".join(f'"{p}"' for p in chunk) + ")"
        rows = sb_get(
            "development_applications",
            params={"planning_portal_number": f"in.{in_clause}",
                    "select": "planning_portal_number,application_status,last_api_seen_at"}
        )
        for row in rows:
            existing[row["planning_portal_number"]] = row

    # 4. Detect status changes → write to history
    history_inserts = []
    for rec in pbsa_records:
        pan = rec["planning_portal_number"]
        old = existing.get(pan)
        if old and old["application_status"] != rec["application_status"]:
            # Calculate days in old status
            old_seen = old.get("last_api_seen_at")
            days_in_old = None
            if old_seen:
                try:
                    old_date = datetime.fromisoformat(old_seen.replace("Z", "+00:00")).date()
                    days_in_old = (date.today() - old_date).days
                except Exception:
                    pass
            history_inserts.append({
                "pan": pan,
                "old_status": old["application_status"],
                "new_status": rec["application_status"],
                "days_in_old_status": days_in_old,
            })
            log.info(f"Status change: {pan} {old['application_status']} → {rec['application_status']}")

    if history_inserts:
        sb_insert("status_history", history_inserts)
        log.info(f"Recorded {len(history_inserts)} status changes")

    # 5. Compute alert flags (needs existing record for NO_UPDATE check)
    for rec in pbsa_records:
        pan = rec["planning_portal_number"]
        rec["alert_flags"] = compute_flags(rec, existing.get(pan))

    # 6. Upsert to Supabase
    chunk_size = 200
    total_upserted = 0
    for i in range(0, len(pbsa_records), chunk_size):
        chunk = pbsa_records[i:i+chunk_size]
        sb_upsert("development_applications", chunk, "planning_portal_number")
        total_upserted += len(chunk)
        log.info(f"Upserted {total_upserted}/{len(pbsa_records)}")

    log.info("=== Done ===")
    log.info(f"Summary: {len(pbsa_records)} PBSA DAs | {len(history_inserts)} status changes")

    # Print alert summary
    flagged = [r for r in pbsa_records if r["alert_flags"]]
    log.info(f"DAs with active alerts: {len(flagged)}")
    for r in flagged[:10]:
        log.info(f"  {r['planning_portal_number']} | {r['application_status']} | {r['alert_flags']} | {r['full_address']}")


if __name__ == "__main__":
    main()
