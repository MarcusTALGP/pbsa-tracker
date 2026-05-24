-- PBSA DA Tracker Schema
-- Run this in Supabase SQL Editor

-- Main DA records table
CREATE TABLE IF NOT EXISTS development_applications (
  id                              SERIAL PRIMARY KEY,
  planning_portal_number          TEXT UNIQUE NOT NULL,   -- PAN-XXXXXX
  council_application_number      TEXT,
  council_name                    TEXT,
  application_type                TEXT,                   -- Development Application / Modification / Review
  application_status              TEXT,
  development_type                TEXT,
  development_category            TEXT,
  development_description         TEXT,
  full_address                    TEXT,
  suburb                          TEXT,
  postcode                        TEXT,
  street_name                     TEXT,
  street_number                   TEXT,
  lot                             TEXT,
  plan_label                      TEXT,
  cost_of_development             NUMERIC,
  number_of_new_dwellings         INTEGER,
  number_of_storeys               INTEGER,
  lodgement_date                  DATE,
  determination_date              DATE,
  determination_authority         TEXT,
  exhibition_start_date           DATE,
  exhibition_end_date             DATE,
  epi_variation_proposed          BOOLEAN,
  epi_variation_approved          BOOLEAN,
  accompanied_by_vpa              BOOLEAN,
  vpa_status                      TEXT,
  subdivision_proposed            BOOLEAN,
  longitude                       NUMERIC,
  latitude                        NUMERIC,
  -- Tracker metadata
  pbsa_confidence                 TEXT,                   -- HIGH / MEDIUM / LOW
  pbsa_match_reason               TEXT,                   -- why we think it's PBSA
  first_seen_at                   TIMESTAMPTZ DEFAULT NOW(),
  last_updated_at                 TIMESTAMPTZ DEFAULT NOW(),
  last_api_seen_at                TIMESTAMPTZ DEFAULT NOW(),
  days_in_current_status          INTEGER,
  alert_flags                     TEXT[],                 -- array of active alert codes
  notes                           TEXT                    -- your manual notes field
);

-- Status change history (append-only)
CREATE TABLE IF NOT EXISTS status_history (
  id                SERIAL PRIMARY KEY,
  pan               TEXT NOT NULL REFERENCES development_applications(planning_portal_number),
  old_status        TEXT,
  new_status        TEXT NOT NULL,
  changed_at        TIMESTAMPTZ DEFAULT NOW(),
  days_in_old_status INTEGER
);

-- Your watchlist (PANs you're actively interested in)
CREATE TABLE IF NOT EXISTS watchlist (
  id          SERIAL PRIMARY KEY,
  pan         TEXT NOT NULL UNIQUE REFERENCES development_applications(planning_portal_number),
  interest    TEXT,     -- 'BUY_LAND' | 'IMPROVE_DA' | 'MONITOR'
  added_at    TIMESTAMPTZ DEFAULT NOW(),
  notes       TEXT
);

-- Indexes for dashboard queries
CREATE INDEX IF NOT EXISTS idx_da_status ON development_applications(application_status);
CREATE INDEX IF NOT EXISTS idx_da_council ON development_applications(council_name);
CREATE INDEX IF NOT EXISTS idx_da_lodgement ON development_applications(lodgement_date);
CREATE INDEX IF NOT EXISTS idx_da_pbsa ON development_applications(pbsa_confidence);
CREATE INDEX IF NOT EXISTS idx_da_flags ON development_applications USING GIN(alert_flags);
CREATE INDEX IF NOT EXISTS idx_status_history_pan ON status_history(pan);

-- Enable Row Level Security but allow public read (dashboard has no auth)
ALTER TABLE development_applications ENABLE ROW LEVEL SECURITY;
ALTER TABLE status_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE watchlist ENABLE ROW LEVEL SECURITY;

-- Public read policy (dashboard can query without login)
CREATE POLICY "Public read DAs" ON development_applications FOR SELECT USING (true);
CREATE POLICY "Public read history" ON status_history FOR SELECT USING (true);
CREATE POLICY "Public read watchlist" ON watchlist FOR SELECT USING (true);

-- Service role can do everything (used by the GitHub Actions fetcher)
CREATE POLICY "Service write DAs" ON development_applications FOR ALL USING (true);
CREATE POLICY "Service write history" ON status_history FOR ALL USING (true);
CREATE POLICY "Service write watchlist" ON watchlist FOR ALL USING (true);

-- Convenience view for dashboard: active PBSA DAs with alert flags
CREATE OR REPLACE VIEW pbsa_active AS
SELECT
  d.*,
  w.interest AS watchlist_interest,
  w.notes AS watchlist_notes,
  CASE
    WHEN 'STALLED_INFO' = ANY(d.alert_flags) THEN 'CRITICAL'
    WHEN 'REJECTED' = ANY(d.alert_flags) THEN 'CRITICAL'
    WHEN 'COURT_APPEAL' = ANY(d.alert_flags) THEN 'CRITICAL'
    WHEN 'WITHDRAWN' = ANY(d.alert_flags) THEN 'WARNING'
    WHEN 'STALLED_ASSESSMENT' = ANY(d.alert_flags) THEN 'WARNING'
    WHEN 'LONG_EXHIBITING' = ANY(d.alert_flags) THEN 'WARNING'
    WHEN 'NO_UPDATE' = ANY(d.alert_flags) THEN 'INFO'
    ELSE 'OK'
  END AS alert_level
FROM development_applications d
LEFT JOIN watchlist w ON w.pan = d.planning_portal_number
WHERE d.application_status NOT IN ('Determined', 'Rejected', 'Withdrawn')
   OR w.pan IS NOT NULL  -- always show watchlisted even if terminal
ORDER BY
  CASE WHEN 'STALLED_INFO' = ANY(d.alert_flags) OR 'REJECTED' = ANY(d.alert_flags) OR 'COURT_APPEAL' = ANY(d.alert_flags) THEN 0
       WHEN 'WITHDRAWN' = ANY(d.alert_flags) OR 'STALLED_ASSESSMENT' = ANY(d.alert_flags) OR 'LONG_EXHIBITING' = ANY(d.alert_flags) THEN 1
       ELSE 2
  END,
  d.cost_of_development DESC NULLS LAST;
