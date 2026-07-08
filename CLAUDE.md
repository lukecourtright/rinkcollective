# CLAUDE.md

## Running the App

```bash
# Windows (opens browser automatically)
run.bat

# Direct
python -m uvicorn main:app --port 8000 --reload
```

App serves at `http://localhost:8000`. No build step needed.

## Setup

```bash
pip install -r requirements.txt
```

## Deployment

Live at `https://barnandbiscuit-production.up.railway.app`. GitHub repo: `github.com/lukecourtright/barnandbiscuit`. Deploys to Railway via `railway.toml`, auto-deploying on push to `main`. The Postgres addon is linked to the web service.

`DATABASE_URL` — Postgres connection string, auto-injected by the Railway Postgres addon. Not required locally: if unset, the app falls back to a local `dev.db` SQLite file (gitignored).

`SECRET_KEY` — signs the session cookie used for login, set as a Railway env var. Not required locally: falls back to an insecure dev default if unset.

`GOOGLE_PLACES_API_KEY` — used by the `/api/photos/{rink_id}/{photo_idx}` proxy to fetch real rink photos from Google Places, and by `scripts/fetch_google_places_data.py` when running the one-time backfill (see Data section below). Not required locally or in prod: if unset, the photo proxy 404s and the frontend falls back to placeholder photos.

`ADMIN_EMAILS` — comma-separated list of email addresses allowed to access `/admin/photos`, the review queue for user-submitted rink photos (approve/reject before they go public). Checked against the logged-in user's email by `require_admin()` in `main.py`. Not required locally or in prod: if unset, nobody can access the admin endpoints (401 if signed out, 403 if signed in as a non-matching email).

`AVANTLINK_AFFILIATE_ID`, `AVANTLINK_WEBSITE_ID` — used by `scripts/fetch_avantlink_products.py` to pull live Equipment product/price data from AvantLink (Pure Hockey's affiliate network — see Equipment section below). Requires an approved AvantLink affiliate account and an approved relationship with the target merchant; the site's first application was **denied** for insufficient traffic/content, so this is on hold pending reapplication. Not required locally or in prod for the app to run — the script is a standalone tool, not called by `main.py` at runtime. Real Equipment offers currently flow in via manual entry instead (`scripts/add_manual_offers.py`, no env vars needed) — see Equipment section below. `AMAZON_PA_API_ACCESS_KEY`/`AMAZON_PA_API_SECRET_KEY`/`AMAZON_PA_API_PARTNER_TAG` (used by `scripts/fetch_amazon_products.py`) are similarly on hold, pending PA-API's own sales-volume gate.

## Brand Name

The brand name is TBD — "HockeyLifers" domain was taken, "Barn & Biscuit" is the current placeholder. To rename:
1. Change `this.BRAND = 'Barn & Biscuit'` near the top of `static/index.html`
2. Update `<title>` in the same file
3. That's it for Rink Finder — all wordmark rendering there derives from `this.BRAND`
4. `static/equipment.html` has its own hardcoded wordmark markup (nav) and `<title>` — it's a standalone page, not driven by `this.BRAND`, so update it separately

---

## Architecture

### File Structure

```
barnbiscuit/
├── main.py                    # FastAPI app + SQLModel models
├── rinks.json                 # Curated rink data — source of truth, synced into the DB on startup
├── rinks_import_template.csv  # CSV template for bulk-adding rinks (fill in, run import script)
├── equipment.json             # Curated gear catalog data — source of truth, synced into the DB on startup
├── scripts/
│   ├── import_rinks_csv.py         # Merges a filled CSV batch (new rinks) into rinks.json
│   ├── export_rinks_csv.py         # Dumps all of rinks.json to one CSV for manual review/editing
│   ├── merge_rinks_csv.py          # Applies a hand-edited export back into rinks.json (updates by id, appends blank-id rows)
│   ├── fetch_google_places_data.py # One-time backfill of real ratings/reviews/photos from Google Places
│   ├── add_manual_offers.py        # Manual entry of real Equipment offers (CSV → EquipmentOffer) — active path while AvantLink/Amazon are blocked, see Equipment section below
│   ├── fetch_avantlink_products.py # AvantLink pipeline for live Equipment offers (search/apply/refresh) — on hold, application denied for now, see Equipment section below
│   └── fetch_amazon_products.py    # Amazon PA-API pipeline for live Equipment offers — same shape, on hold pending PA-API's sales-volume gate (see Equipment section below)
├── dev.db                     # Local SQLite fallback when DATABASE_URL is unset (gitignored)
├── requirements.txt
├── railway.toml
├── run.bat
└── static/
    ├── index.html           # Entire frontend SPA (Rink Finder)
    ├── equipment.html       # Standalone page — gear catalog/compare/detail-drawer app (affiliate shopping, see Equipment section below)
    ├── admin.html           # Standalone page — review/approve/reject pending user-submitted photos (see ADMIN_EMAILS above)
    ├── brand-tokens.css     # CSS custom properties (Neon Night palette)
    └── logo/                # Favicons + SVG marks
```

### Backend (`main.py`)

Data is stored in a database (Postgres in production via Railway addon, local SQLite fallback otherwise) accessed through SQLModel. `rinks.json` remains the human/AI-edited source of truth — on every startup, `SQLModel.metadata.create_all()` creates tables if missing, `ensure_new_columns()` `ALTER TABLE ADD COLUMN`s anything the `Rink`/`Equipment`/`EquipmentOffer`/`EquipmentPriceSnapshot` models have that their *already-existing* tables don't (since `create_all()` only creates missing tables, not missing columns — without this, adding a field to any model crashes startup against a live Postgres table with `UndefinedColumn`; it loops over all four tables so new columns on any are covered), then `sync_rinks_from_file()` and `sync_equipment_from_file()` each upsert (by `id`) every row from their JSON file into the matching table and delete any row whose `id` is no longer in the file. Pushing an updated `rinks.json`/`equipment.json` to `main` (additions, edits, *and* removals) is enough to update production data on the next deploy — and adding a new column to `Rink`/`Equipment` itself is safe to deploy directly, no manual migration step needed. `EquipmentOffer`/`EquipmentPriceSnapshot` are the one exception to this file-is-truth pattern — see Equipment section below.

- `GET /` → serves `static/index.html`
- `GET /api/rinks` → queries the `Rink` table, returns all rows as JSON (same shape as before)
- `GET /equipment` → serves `static/equipment.html`, the gear catalog page (not part of the `RinkFinder` SPA — same standalone-page pattern as `/admin/photos`)
- `GET /api/equipment` → queries the `Equipment` table, and for each product merges in any matching `EquipmentOffer` rows via `serialize_equipment()` — if a product has zero offers (the common case today), it's served exactly as stored in `equipment.json`; if it has one or more live offers, `retailers`/`priceHistory`/`wasPrice`/`priceIsGood`/`deal`/`note` are computed from those instead, overriding the mock values without mutating them. The catalog is small (~37 rows) so all data ships at once; filtering/sorting/comparing happens client-side, same approach `RinkFinder` uses for the much larger rinks list
- `GET /api/photos/{rink_id}/{photo_idx}` → looks up `rink.photos[photo_idx].ref` (a Google Places photo resource name) and redirects to a freshly-fetched Google-hosted image URL, with a `Cache-Control` header so browsers don't re-hit this (and therefore Google) on every load. Requires `GOOGLE_PLACES_API_KEY`; 404s otherwise or if the rink/index doesn't exist, which the frontend treats as "no real photo" and falls back to placeholders
- `POST /api/rinks/submit` → inserts community-submitted rinks into the `PendingRink` table (`id`, `submittedAt`, raw `data` JSON blob) — not public until moderated, no validation yet
- `POST /api/auth/signup`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` → email/password auth against the `User` table (`id`, `email`, `passwordHash` (bcrypt), `displayName`, `createdAt`). Login state is a signed, httponly session cookie (Starlette `SessionMiddleware`, see `SECRET_KEY` above) holding `user_id` — no tokens handled in JS.
- `POST /api/rinks/{rink_id}/photos` → real user photo upload (multipart `file` + optional `caption`), requires sign-in (401 otherwise). Bytes are stored directly in the `RinkPhoto` table (`id`, `rinkId`, `userId`, `data` as `LargeBinary`, `contentType`, `caption`, `status` (`"pending"` → `"approved"`), `submittedAt`) — no external storage/CDN, same DB as everything else. Validates the rink exists (404), caps uploads at 8MB (413), and sniffs magic bytes to confirm real JPEG/PNG regardless of the client-supplied content type (400 otherwise). Every upload lands as `status="pending"` and is invisible everywhere until an admin approves it.
- `GET /api/rinks/{rink_id}/photos` → public list of that rink's `status="approved"` `RinkPhoto` rows (`[{id, caption}]`) — what the frontend merges into a rink's photo gallery, see `getPhotos()` below
- `GET /api/user-photos/{photo_id}` → serves the raw bytes of an approved photo (404 if not approved/doesn't exist), with the same `Cache-Control: public, max-age=3600` treatment as the Google photo proxy above
- `GET /admin/photos` → serves `static/admin.html`, a small standalone review page (not part of the `RinkFinder` SPA)
- `GET /api/admin/photos`, `GET /api/admin/photos/{photo_id}/image`, `POST /api/admin/photos/{photo_id}/approve`, `POST /api/admin/photos/{photo_id}/reject` → list/view/approve/reject pending `RinkPhoto` rows, gated by `require_admin()` (see `ADMIN_EMAILS` above). Reject hard-deletes the row — rejected photos aren't retained.
- `/static` → static file mount for CSS, logos, etc.

### Frontend (`static/index.html`)

Vanilla JS SPA — no bundler, no framework.

**`RinkFinder` class** manages all state and rendering:
- `this.rinks` — fetched from `/api/rinks` on init
- `this.state` — single state object (search, filters, selectedRinkId, drawerOpen, activeTab, locationStatus, mobileView, checkinsById, checkinConfirm, heroIdx, myCheckins, myReviews, communityPhotos, reviewOpen, reviewRating, reviewToast, photoToast, currentUser, showAuth, authMode, showAddPhoto, photoDraftFile, photoPreviewUrl, photoUploadError, etc.)
- `setState(partial | fn)` — merges partial state and calls `render(prev)`
- `render(prev)` — diffs against prev state, updates the DOM in targeted sections

**Three dynamic render sections** (rebuilt via `innerHTML` on change):
- `#rink-list` + `#mobile-rink-list` — rink cards, rebuilt on filter/search/selection changes
- `#drawer-body` — Info/Photos/Reviews/Schedule tab content, rebuilt on selection/tab/checkin changes (Schedule is a UI label only — it still renders `rink.events` via `renderEventsTab()`)
- Modals — toggled via `display` on `showReport`/`showAddRink`/`showAuth`/`showAddPhoto` state. The auth modal doubles as sign-in/sign-up, switching via `authMode` (`updateAuthUI()` toggles the display-name field, title, and error text). The Add Photo modal (`renderAddPhotoModal()`) has an empty state (dropzone — drag/drop or browse, JPG/PNG only, wired via inline `ondragover`/`ondragleave`/`ondrop` rather than `addEventListener` since the node is recreated by `innerHTML` each time) and a filled state (object-URL preview via `photoPreviewUrl`, Remove button, optional caption) once a file is chosen via `handlePhotoFile()`

**All other DOM updates** (location label, toggle state, filter chip active class, distance label, count) are targeted property sets, not full re-renders.

**Detail drawer structure** (`renderDrawer()`, `static/index.html`):
- Fixed **photo hero** (168px, gradient scrim) with a "{N} photos" / "No photos yet" chip and close button, overlaid rink name/badges — `getPhotos()` merges Google-sourced photos from `rink.photos` (served via the `/api/photos` proxy, with a small attribution caption) with approved community uploads from `state.communityPhotos[rink.id]` (served via `/api/user-photos/{id}` — fetched from `GET /api/rinks/{id}/photos` when a rink is selected, see `selectRink()`); only when *both* are empty does it fall back to `rinkPlaceholder(seed)`, a branded top-down rink-diagram SVG generated inline (no network request) and seeded by `rink.id` so a rink's set of `BASE_PHOTO_COUNT` (4) tiles — and each rink's placeholder set relative to others — reads as visually distinct (accent color + puck side vary by seed). These placeholder entries are tagged `placeholder: true`, which the drawer uses to: show the hero at full opacity instead of 0.85, swap the chip/footnote copy to the "no photos yet" variant, and hide the thumb rail's "+N" overflow tile (only shown for real photo counts). The thumb rail's and Photos tab's "Add" tiles call `openAddPhotoModal()`, which gates on `currentUser` (opens sign-in instead if signed out) — a real upload goes through `POST /api/rinks/{rink_id}/photos` and lands in a moderation queue (see Backend above), so `submitAddPhoto()` does *not* append it to the gallery or feature it as the hero; it just closes the modal, switches to the Photos tab, and shows a "submitted for review" toast (`photoToast`) so the confirmation is visible regardless of which tab the upload started from
- Below the hero, one scrollable container holds, in order: **thumb rail** (`renderThumbRail()` — click a thumb or the Photos tab to re-feature it as the hero via `heroIdx`), a **check-in + Directions row** (`renderCheckinRow()` — visible across all tabs, unlike the old Info-tab-only button), a **live check-in feed card** (`renderFeedCard()` — deterministic mock rows from `getMockFeed()`, plus a persistent "You" row once `myCheckins[rinkId]` is set), then a **sticky tab bar** (Info/Photos/Reviews/Schedule) and the tab body
- `myCheckins`/`myReviews` are session-only client state overlaid on top of the persisted `rinks.json` data (same pattern as the pre-existing `checkinsById`) — nothing here is sent to the backend. Photos are the exception: uploads are real and server-persisted (see Backend above), just not visible until approved
- The Reviews tab's composer reads its `<textarea>` via `document.getElementById('review-text').value` only at submit time (not mirrored into `state` on every keystroke) to avoid `innerHTML`-driven focus loss, since `renderDrawer()` is not diffed/keyed

**Map** (Leaflet.js 1.9.4 + CartoDB Dark Matter tiles, free, no API key):
- Custom teardrop `divIcon` pins: cyan default, gold when selected
- `updateMarkers()` called on filter/search/selection changes — adds/removes markers from the map to match the filtered list
- `map.invalidateSize()` called after drawer opens/closes so Leaflet redraws to the new viewport width
- `map.panTo()` called when a rink is selected

**Geolocation flow:**
1. `requestLocation()` called on init and on nav button click
2. On grant: flies to user coords, zoom 10, adds cyan circle marker, sorts by distance
3. On deny: "Location Off" label, distance filter/sort gracefully hidden

**`openNow`** is computed dynamically in the browser from `hours[day]` + current local time — not stored in `rinks.json`.

**Responsive breakpoint:** 768px
- Below: sidebar hidden, nav links hidden, floating Map/List toggle, full-screen list overlay (`#mobile-list`)
- Above: 355px sidebar, 400px detail drawer

### Data (`rinks.json`)

Source of truth for rink data — edit by hand to add/remove/update. Synced into the `Rink` table (Postgres/SQLite, see Backend above) on every app startup: rows are upserted by `id`, and any DB row whose `id` is no longer present in `rinks.json` is deleted, so removals in the file propagate too. `openNow` is not stored — it's derived at runtime in the browser.

**Current count:** ~658 rinks, covering all 50 states. Built up via state-by-state CSV batches from 2026-06-30 through 2026-07-02, plus a gap-finding merge pass — see git log for the batch-by-batch history.

**Bulk import workflow:**
1. Copy `rinks_import_template.csv`, fill in one region's worth of rinks, save as a new file.
2. Run `python scripts/import_rinks_csv.py path/to/batch.csv` — appends to `rinks.json` with sequential `id`s.
3. Push `rinks.json` to `main` → Railway auto-deploys and syncs to Postgres on startup.

**Manual spot-check / gap-finding workflow:**
1. Run `python scripts/export_rinks_csv.py` — dumps every rink to `rinks_full_export.csv`, sorted by state/city/name for easy scanning.
2. Edit that one file by hand: correct any row's fields, or add new rows with a blank `id` for rinks that are missing entirely.
3. Run `python scripts/merge_rinks_csv.py rinks_full_export.csv` — updates existing rinks by `id` (only reports fields that actually changed) and appends blank-`id` rows as new rinks. Never deletes; round-trips with zero diff if nothing was edited.
4. Push `rinks.json` to `main` as usual.

**Google Places backfill workflow (one-time, not an ongoing sync):**
1. Set `GOOGLE_PLACES_API_KEY` locally (requires a Google Cloud project with Places API (New) enabled and billing attached).
2. Run `python scripts/fetch_google_places_data.py match --limit 10` (try a small batch first) — text-searches each unmatched rink by name+address and writes candidates to `rinks_google_match_review.csv`. No `rinks.json` writes yet.
3. Spot-check that CSV by hand — blank out `matched_place_id` for any wrong or missing matches.
4. Run `python scripts/fetch_google_places_data.py apply rinks_google_match_review.csv` — pulls real `rating`/`reviewCount`/`reviews`/`photos` from Place Details for every remaining match and merges into `rinks.json` by `id` (Google reviews get a `"source": "google"` marker and are prepended ahead of any hand-curated ones; `googlePlaceId` is stored so re-running `match` skips already-matched rinks).
5. Drop the `--limit` flag to run the full batch once satisfied, then push `rinks.json` to `main` as usual. Photo images themselves aren't downloaded here — only each photo's Google resource name is recorded, fetched live later by the `/api/photos` proxy (see Backend section).

**CSV field notes (learned from IL/WI batch):**
- `type` — use `NHL`, `OLYMPIC`, `SYNTHETIC`, or `STANDARD`. `Indoor` also accepted (maps to `STANDARD`). Use `OLYMPIC` for rinks that explicitly have an Olympic-size (200×100 ft) sheet. Any other value (e.g. `Arena`, `Ice Rink`) silently falls back to `STANDARD` in the import script — prefer setting `STANDARD` explicitly in the CSV for multi-purpose/pro arenas rather than relying on the fallback.
- `amenities` — comma-separated or semicolon-separated, both work (auto-detected).
- `website` — `https://` and `http://` prefixes are stripped automatically.
- `hours_*` — use `"Varies"` when hours change seasonally/weekly (stored as-is and displayed). Leave blank to default to `"Call for hours"`.
- `events`/`reviews`/`rating`/`reviewCount`/`checkins` — not in the CSV. Rating/counts get randomized illustrative placeholders; events/reviews start empty.
- Watch for the same address appearing twice under different names (e.g. a rink under an old name and its current naming-rights name) — that's usually one rink double-listed, not two distinct facilities. Co-located but genuinely distinct facilities (e.g. a pro team's game arena and a separate public rec rink in the same complex) are fine to keep as separate entries.

**Schema** (mirrors the `Rink` SQLModel in `main.py` field-for-field — `hours`/`amenities`/`events`/`reviews`/`photos` are stored as JSON columns, everything else as real columns):
```json
{
  "id": 1,
  "name": "Rink Name",
  "address": "123 Ice Ln",
  "city": "Boston",
  "state": "MA",
  "lat": 42.35,
  "lng": -71.15,
  "type": "NHL",           // "NHL" | "OLYMPIC" | "SYNTHETIC" | "STANDARD"
  "isPublic": true,
  "rating": 4.6,
  "reviewCount": 312,
  "phone": "(617) 555-0000",
  "website": "example.com",   // without https://
  "checkins": 847,
  "hours": {
    "Mon": "6am–10pm",
    "Tue": "6am–10pm",
    "Wed": "6am–10pm",
    "Thu": "6am–10pm",
    "Fri": "6am–10pm",
    "Sat": "8am–8pm",
    "Sun": "Closed"           // or "Private" for members-only
  },
  "amenities": ["Pro Shop", "Locker Rooms"],
  "events": [{ "title": "Public Skate", "date": "Sat 1–3 PM" }],
  "reviews": [{ "author": "Name", "rating": 5, "text": "Great rink.", "date": "2d ago" }],
  "photos": [{ "ref": "places/ChIJ.../photos/AUc7...", "attribution": "Jane D." }],
  "googlePlaceId": "ChIJ..."
}
```
`reviews` entries pulled from Google get an additional `"source": "google"` key (absent = hand-curated). `photos`/`googlePlaceId` are populated by `scripts/fetch_google_places_data.py` — see the backfill workflow above — and are omitted entirely for rinks that haven't been matched yet.

### Equipment (`static/equipment.html`, `equipment.json`)

Full-screen gear catalog/comparison app — Barn & Biscuit's second feature alongside Rink Finder, and its first revenue path (affiliate shopping links). Standalone page at `GET /equipment`, sharing only the nav bar and brand tokens with Rink Finder; it has its own `class Equipment { state, setState(), render() }` (same pattern as `RinkFinder`, not the thin `admin.html` template-string style) because its UI — filters, sort, a multi-select compare tray, a compare overlay, a detail drawer — is comparably complex.

- **State** (`this.state` in `static/equipment.html`): `products` (fetched from `/api/equipment` on init), `category`, `search`, `brands` (checked filters), `maxPrice`, `minRating`, `sort`, `compare` (selected product ids, max 4), `compareOpen`, `detailId`.
- **Sidebar** — 10 fixed categories (Skates, Sticks, Helmets, Gloves, Shoulder pads, Elbow pads, Shin guards, Pants, Bags, Goalie gear) each with a live product count; brand checkboxes generated per-category; a max-price slider bounded by that category's cheapest/priciest product; min-rating pills. Selecting a category resets brand/price/rating filters and clears the compare tray (`selectCategory()`) — compare is intentionally single-category.
- **Catalog cards** — image tile (category SVG icon placeholder — swap for `imageUrl` once real product photography exists), spec chips, star rating, featured review quote, best price with a synthetic 90-day sparkline (`sparkline()`), a primary "Buy at {retailer}" button, up to 2 other retailer prices, and a Compare toggle.
- **Compare** — sticky bottom tray appears at 1+ selected, "Compare N →" enables at 2+, opens a modal (`renderCompareOverlay()`) with a CSS-grid spec table; Best price (lowest), Rating (highest), and Weight (lowest, parsed numerically) get a gold "BEST" flag via `BEST_NUMERIC_SPECS`.
- **Detail drawer** (`renderDrawer()`) — slides in from the right on card click; price panel with all retailers ranked cheapest-first, the FTC-style affiliate disclosure line, full specs table, and a reviews summary (rating histogram) + list.
- **Buy/View links are real** once a product has a live `EquipmentOffer` (see Live Offers below) — `href` is the offer's real url, opened in a new tab (`target="_blank" rel="noopener sponsored"`), via `app.buyClick()` (reads `href` off the DOM rather than interpolating the url into the `onclick` string, to avoid breaking the attribute on urls containing quotes). Products with no live offer yet still render `href="#"` from the mock data, and `buyClick()` calls `preventDefault()` in that specific case so they stay inert — no special-casing needed elsewhere.

### Live Offers (`EquipmentOffer`, `EquipmentPriceSnapshot`, `scripts/add_manual_offers.py`)

The path from mock catalog to a real, purchasable one: `equipment.json` stays the source of truth for *curated* fields (`category`, `brand`, `name`, `specs`, `imageUrl`, `featuredQuote`, `reviewList`, `rating`, `reviewCount`) exactly as today, but pricing/retailer data can now come from a live source instead. Two new tables, populated only by the fetch/entry scripts below and never touched by `sync_equipment_from_file()`:
- **`EquipmentOffer`** — one row per (product, retailer) live listing: `equipmentId`, `retailerName`, `network` (`"manual"`, `"avantlink"`, `"amazon-pa-api"`), `sourceProductId` (SKU/ASIN, or a synthetic id for manual entries), `sourceMerchantId` (network-assigned merchant/advertiser id, needed to re-look-up a specific offer directly rather than re-searching by keyword — not every network needs this, so it's nullable), `price`, `url`, `inStock`, `lastCheckedAt`.
- **`EquipmentPriceSnapshot`** — one row per price check of a given offer over time (`equipmentOfferId`, `price`, `checkedAt`), so `priceHistory`/`wasPrice`/`deal` can reflect a real trend instead of synthetic data.

This split exists specifically so a routine `equipment.json` deploy can never clobber a live-refreshed price back to a stale mock value — `GET /api/equipment`'s `serialize_equipment()` only overrides a product's pricing fields when it has `EquipmentOffer` rows; everything else still serves the mock values as-is.

**Manual entry (`network="manual"`) is the active path** — both automated networks are currently blocked (see below), so `scripts/add_manual_offers.py` is how real offers get in today: a human looks up a real product on a retailer's site, copies the current price and product page URL by hand into a CSV (`id` — existing Equipment id, blank to create a new product — plus `retailer_name`, `price`, `url`, and `category`/`brand`/`name`/`image` if creating new), and the script upserts the matching `EquipmentOffer`/`EquipmentPriceSnapshot` directly (no API call, no affiliate tag needed on the url itself — see Sovrn Commerce below). Re-running with the same `id`+`retailer_name` updates that offer in place, so it also serves as the manual "refresh" step until an automated network takes over for a given product.

**Sovrn Commerce (formerly VigLink)** is the planned monetization layer on top of these plain manual links — not yet signed up for. Unlike every network above, it requires no traffic/content review and no per-retailer application: you install one JS snippet site-wide, and it automatically rewrites any outbound link to a participating merchant into a tracked affiliate link at click time. That means the manual entry workflow doesn't need to change once it's live — the same plain product URLs just start earning commission. Next step here: sign up, get the real embed snippet, add it to `static/equipment.html` (and verify site ownership if required, same pattern as the AvantLink verification tag once was — see git history).

**AvantLink** — applied via Pure Hockey's affiliate link, but the application (id 1621005) was **denied**: AvantLink requires established site traffic/content, which a brand-new site doesn't have yet. Their own guidance is to reapply after building traffic/content/backlinks — ironically, a working Equipment section (even on manual + Sovrn Commerce links) is exactly the kind of content that helps clear that bar next time. `scripts/fetch_avantlink_products.py` (the `search`/`apply`/`refresh` pipeline against AvantLink's `ProductSearch`/`ProductPriceCheck` APIs) is kept as-is, unrun, for whenever reapplication succeeds. Requires `AVANTLINK_AFFILIATE_ID`/`AVANTLINK_WEBSITE_ID`.

**Amazon PA-API** — also on hold: requires 10 sales in 30 days before granting API access, a chicken-and-egg gate independent of the traffic issue above. `scripts/fetch_amazon_products.py` is kept as-is, unrun, for whenever that clears. Requires `AMAZON_PA_API_ACCESS_KEY`/`AMAZON_PA_API_SECRET_KEY`/`AMAZON_PA_API_PARTNER_TAG`.

### Data (`equipment.json`)

Source of truth for the gear catalog's curated fields, same sync-on-startup treatment as `rinks.json` (see Backend above): upserted by `id` into the `Equipment` table, stale rows deleted. Pricing/retailer fields (`retailers`, `priceHistory`, `wasPrice`, `priceIsGood`, `deal`, `note`) are stored here too and still serve as the fallback for any product with no live `EquipmentOffer` — see Live Offers above. **All 37 products are illustrative mock data** (ported from the design handoff prototype) — brands, model names, specs, prices, and reviews are not real and must not be treated as live pricing, except for whichever products a `fetch_amazon_products.py apply` run has since matched to a live offer.

**Schema** (mirrors the `Equipment` SQLModel in `main.py`):
```json
{
  "id": 101,
  "category": "Sticks",
  "brand": "Bauer",
  "name": "Vapor Hyperlite 2",
  "rating": 4.7,
  "reviewCount": 212,
  "imageUrl": null,
  "deal": "−12%",
  "note": "Lowest in 90 days",
  "priceIsGood": true,
  "wasPrice": 329,
  "priceHistory": [329.0, 322.33, "..."],
  "featuredQuote": "Lightning-quick release — the snap is unreal.",
  "retailers": [{ "name": "Pure Hockey", "price": 289, "url": "#", "inStock": true }],
  "specs": [{ "label": "Flex", "value": "77" }],
  "reviewList": [{ "author": "Mike D.", "rating": 5, "text": "...", "date": "4d ago" }]
}
```
`deal`/`wasPrice` are omitted (`null`) for products with no active deal. `priceIsGood` drives the sparkline/note color (green "positive signal" vs neutral "Stable price"). Specs are consistent within a category (same label set) so the compare table aligns.

### Brand System (`static/brand-tokens.css`, `static/logo/`)

- Copied from `C:\Users\lukec\Desktop\SpendTools\design_handoff_brand_system\` — do not edit in place; re-copy from source if the design system is updated
- Dark theme activated by `<html data-theme="dark">` on the root element
- All colors in `index.html` use `var(--token-name)` from this file
- Key tokens: `--bg` (#0A0E1A), `--surface` (#131A2B), `--surface-2` (#1C2540), `--border` (#2A3450), `--color-primary` (cyan #14CFCF), `--font-display` (Space Grotesk), `--font-body` (Hanken Grotesk), `--font-mono` (Space Mono)

---

## Not Yet Implemented

- Ongoing Google Places sync — the backfill (`scripts/fetch_google_places_data.py`) is a one-time pull, not a scheduled refresh; rinks added after a backfill run (or newly opened Google listings) need a manual re-run to pick up real ratings/reviews/photos, and unmatched rinks keep placeholder content indefinitely until then
- Admin UI for moderating community-submitted rinks (sit in the `PendingRink` table, unvalidated) — the pending-photo review page (`/admin/photos`, gated on `ADMIN_EMAILS`, see Backend above) is a close precedent to extend for this: same `require_admin()` gate, same list/approve/reject shape, just against `PendingRink` instead of `RinkPhoto`
- Server-persisted *user-submitted* check-ins and reviews (session-only in v1 — see `myCheckins`/`myReviews` above) — accounts now exist to attribute these to, but neither is wired to the `User` table yet. Photos got this treatment already (`POST /api/rinks/{rink_id}/photos` → `RinkPhoto` table → `/admin/photos` review queue → public once approved) and would be the template to follow. This is separate from the Google-sourced reviews/photos in `rinks.json`, which are real but were pulled once, not submitted by app users
- Real schema migrations (no Alembic) — `ensure_new_columns()` in `main.py` covers the common case of adding a new nullable column, but column renames/type changes/drops still have no automated path and would need a manual `ALTER TABLE` against the Railway Postgres addon
- Community section — **temporarily removed from the nav** (not deleted) in favor of Equipment, a business decision to prioritize the affiliate revenue path. The dead `<a>` link is commented out in `static/index.html`'s nav (`static/equipment.html` never had it); there's no actual Community route/component to restore beyond that. News is unaffected (nav link present but inactive, as before)
- Real affiliate program for Equipment — both automated networks are on hold (AvantLink denied the site's application for insufficient traffic/content; Amazon PA-API requires 10 sales in 30 days before granting API access), so real offers currently come in via manual entry (`scripts/add_manual_offers.py`, `network="manual"`) with plain, untagged retailer URLs. Buy/View links in `static/equipment.html` now navigate for real once a product has a live offer (see Live Offers above), but nothing is monetized yet — Sovrn Commerce (formerly VigLink), which needs no traffic review or per-retailer application, is the planned way to auto-monetize those existing links, but the site isn't signed up yet. The affiliate disclosure copy in the detail drawer is still placeholder text pending legal review regardless. Once the site has more real traffic/content (helped by Equipment itself actually working), reapply to AvantLink and revisit Amazon PA-API; other retailers/networks (HockeyMonkey via Rakuten/Pepperjam, Ice Warehouse via Awin) aren't evaluated yet. Matching the same physical product across multiple retailers/networks has no reliable automated path regardless of network — it needs the same hand-reviewed CSV approach these scripts already use.
- Equipment mobile layout — the catalog/sidebar/compare-table/drawer are desktop-only (matches the design handoff, which explicitly flagged mobile as an unscoped follow-up)
- "Submit an Event" button (UI only, no backend) — "Write a Review" now has a working session-local composer (see above), just not server-persisted
