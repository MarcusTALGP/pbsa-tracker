# PBSA DA Tracker

Tracks all Purpose Built Student Accommodation Development Applications across NSW,
sourced daily from the NSW Planning Portal Open Data API.

---

## Setup (~30 minutes total)

### 1. Create accounts

**GitHub** → https://github.com/signup (free)
**Supabase** → https://supabase.com → "Start your project" → "Continue with GitHub"

In Supabase, create a project:
- Name: `pbsa-tracker`
- Region: Southeast Asia (Singapore)
- Save your database password somewhere safe

---

### 2. Set up the database

1. In Supabase, click **SQL Editor** in the left sidebar
2. Copy the entire contents of `supabase_schema.sql`
3. Paste it into the editor and click **Run**
4. You should see "Success. No rows returned"

---

### 3. Create a GitHub repository

1. Go to https://github.com/new
2. Name it `pbsa-tracker`
3. Set to **Private** (your business logic)
4. Click **Create repository**

Upload all these files maintaining the folder structure:
```
pbsa-tracker/
├── .github/
│   └── workflows/
│       └── fetch.yml
├── fetcher/
│   └── fetch.py
├── docs/
│   └── index.html          ← the dashboard
└── README.md
```

The easiest way: click "uploading an existing file" on the repo page, then drag and drop.

---

### 4. Add Supabase credentials to GitHub

Your fetcher needs to talk to Supabase. Store the credentials as GitHub Secrets
(never commit them to code).

1. In Supabase → **Settings** → **API**, copy:
   - **Project URL** (looks like `https://abcdefgh.supabase.co`)
   - **service_role** key (under "Project API keys" — NOT the anon key)

2. In GitHub → your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:
   - Name: `SUPABASE_URL` → paste your Project URL
   - Name: `SUPABASE_KEY` → paste your service_role key

---

### 5. Configure the dashboard

1. Open `docs/index.html` in a text editor
2. Find these two lines near the bottom:
   ```javascript
   const SUPABASE_URL      = "YOUR_SUPABASE_URL";
   const SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY";
   ```
3. Replace with your actual values:
   - `SUPABASE_URL` = same Project URL as above
   - `SUPABASE_ANON_KEY` = the **anon/public** key (safe to expose in HTML)
4. Save and re-upload to GitHub

---

### 6. Enable GitHub Pages (your dashboard URL)

1. GitHub → your repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: `/docs`
4. Click **Save**
5. After ~2 minutes, your dashboard is live at:
   `https://YOUR-GITHUB-USERNAME.github.io/pbsa-tracker/`

---

### 7. Run your first sync

**Option A: Full historical sync** (recommended first time — fetches all DAs from 2019)
1. GitHub → your repo → **Actions** tab
2. Click **Fetch PBSA DAs** → **Run workflow**
3. Set `Full sync from 2019?` to `true`
4. Click **Run workflow**
5. This will take 5–15 minutes. Watch the logs.

**Option B: Daily incremental** (runs automatically every day at 6am AEST)
No action needed — just wait for tomorrow morning.

---

## Day-to-day use

**Dashboard**: open `https://YOUR-USERNAME.github.io/pbsa-tracker/`

**Filters**:
- Filter by status, council, PBSA confidence, alert type
- Search by address, PAN number, or keyword
- ⚡ "Alerts only" — shows only DAs with active flags
- ★ "Watchlist only" — shows only sites you're tracking

**Click any row** to open the detail panel:
- Full DA details, description, lot/plan info
- Status change history
- Your notes field
- Link to the live Planning Portal page

---

## Alert flags explained

| Flag | Meaning | Threshold |
|------|---------|-----------|
| `STALLED_INFO` | "Additional Information Requested" for too long | 30 days |
| `STALLED_ASSESSMENT` | "Under Assessment" for too long | 90 days |
| `LONG_EXHIBITING` | On exhibition for too long (objections likely) | 60 days |
| `REJECTED` | DA was rejected — site may be for sale |  |
| `WITHDRAWN` | Applicant withdrew — often means trouble |  |
| `COURT_APPEAL` | Applicant or objector has gone to court |  |
| `DEFERRED` | Deferred commencement consent |  |
| `NO_UPDATE` | Active DA, no API update in 60 days |  |

---

## Investment logic

**Best buying signals:**
1. `REJECTED` + HIGH confidence PBSA → Owner has a site zoned for PBSA but DA failed. May sell.
2. `STALLED_INFO` > 60 days → Applicant struggling with council. Opportunity to step in.
3. `WITHDRAWN` → They gave up. Site still has development potential.
4. `STALLED_ASSESSMENT` > 180 days → Something is wrong. Check the portal for objections.

**Best improvement signals:**
1. You already own the site → watch for `STALLED_INFO` and respond quickly
2. `LONG_EXHIBITING` → objections are being lodged; may need a DA amendment strategy

---

## PBSA confidence levels

| Level | Meaning |
|-------|---------|
| HIGH | "student accommodation", "PBSA", "student housing" in description/type |
| MEDIUM | "boarding house", "co-living", "university accommodation" |
| LOW | Inferred: large commercial/residential development ($5M+), or "serviced apartment", "micro-apartment" |

The LOW confidence bucket will have false positives — it's intentionally broad.
Use your judgment when reviewing LOW results.

---

## Troubleshooting

**Dashboard shows "Setup required"** → You haven't replaced the placeholder credentials in `index.html`

**GitHub Action fails** → Check the Actions log. Most common cause: wrong SUPABASE_KEY (make sure it's the service_role key, not anon)

**No PBSA results after sync** → The API may use different field names than expected. Check the Action log for "Total DAs fetched" — if that's 0, there's a date range issue. Try running with `DAYS_BACK=365`.

**API "Required parameters" error** → The NSW Planning Portal API occasionally changes its required params. Check the logs and open an issue.

---

## Files

```
.github/workflows/fetch.yml   GitHub Actions cron job (runs daily)
fetcher/fetch.py               Main fetcher — calls NSW API, upserts to Supabase
docs/index.html                Dashboard (served via GitHub Pages)
supabase_schema.sql            Database schema (run once in Supabase SQL Editor)
README.md                      This file
```
