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

Live at `https://rinkcollective.com` (also resolves at `https://www.rinkcollective.com` and the underlying `https://rinkcollective-production.up.railway.app`). GitHub repo: `github.com/lukecourtright/rinkcollective`. Deploys to Railway via `railway.toml`, auto-deploying on push to `main`. The Postgres addon is linked to the web service.

`railway.toml`'s `startCommand` runs uvicorn with `--proxy-headers --forwarded-allow-ips='*'` — required because Railway terminates TLS at its own edge and forwards plain HTTP to the container, so without this, uvicorn (which by default only trusts `X-Forwarded-Proto`/`X-Forwarded-For` from a loopback peer) sees every request as `http`, not `https`. This didn't matter until the app needed to build an absolute callback URL for Google Sign-In (`request.base_url` — see Account & Onboarding section below): without the flag, that URL came out as `http://rinkcollective.com/api/auth/google/callback`, which doesn't match the `https://` redirect URI registered in Google Cloud Console and fails with `redirect_uri_mismatch`. `*` (rather than a specific IP) is safe here since the container is only reachable via Railway's own routing, never directly from the internet.

`DATABASE_URL` — Postgres connection string, auto-injected by the Railway Postgres addon. Not required locally: if unset, the app falls back to a local `dev.db` SQLite file (gitignored).

`SECRET_KEY` — signs the session cookie used for login, set as a Railway env var. Not required locally: falls back to an insecure dev default if unset.

`GOOGLE_PLACES_API_KEY` — used by the `/api/photos/{rink_id}/{photo_idx}` proxy to fetch real rink photos from Google Places, and by `scripts/fetch_google_places_data.py` when running the one-time backfill (see Data section below). Not required locally or in prod: if unset, the photo proxy 404s and the frontend falls back to placeholder photos.

`ADMIN_EMAILS` — comma-separated list of email addresses allowed to access `/admin`, the site owner's Admin Console (photo moderation, rink editing, member directory — see Admin Console section below). Checked against the logged-in user's email by `require_admin()` in `main.py`. Not required locally or in prod: if unset, nobody can access the admin endpoints (401 if signed out, 403 if signed in as a non-matching email).

`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` — power the "Continue with Google" button (see Account & Onboarding section below). Not required locally or in prod: if unset, `GET /api/auth/google/login` redirects straight back to `/rinks?auth=login&error=google_unavailable` instead of erroring, same graceful-degrade convention as `GOOGLE_PLACES_API_KEY`. No redirect-URI env var — it's derived from the incoming request's own host at request time, so local dev and every prod domain work without extra config; each domain the app is actually reachable at (`rinkcollective.com`, `www.rinkcollective.com`, `rinkcollective-production.up.railway.app`) still needs its own `/api/auth/google/callback` URL added to the OAuth client's Authorized redirect URIs in Google Cloud Console, or sign-in fails for visitors on whichever domain isn't registered. The OAuth consent screen's own Publishing status (Testing vs. In production) is a separate, non-env-var gate — see Not Yet Implemented below.

`AVANTLINK_AFFILIATE_ID`, `AVANTLINK_WEBSITE_ID` — used by `scripts/fetch_avantlink_products.py` to pull live Equipment product/price data from AvantLink (Pure Hockey's affiliate network — see Equipment section below). Requires an approved AvantLink affiliate account and an approved relationship with the target merchant; the site's first application was **denied** for insufficient traffic/content, so this is on hold pending reapplication. Not required locally or in prod for the app to run — the script is a standalone tool, not called by `main.py` at runtime. Real Equipment offers currently flow in via manual entry instead (`scripts/add_manual_offers.py`, no env vars needed) — see Equipment section below. `AMAZON_PA_API_ACCESS_KEY`/`AMAZON_PA_API_SECRET_KEY`/`AMAZON_PA_API_PARTNER_TAG` (used by `scripts/fetch_amazon_products.py`) are similarly on hold, pending PA-API's own sales-volume gate.

## Brand Name

The brand is **RinkCollective** (domain purchased) — "HockeyLifers" was taken, "Barn & Biscuit" was the placeholder used before this rename. The wordmark is written as one solid word, no space — "Rink" in the page's text color, "Collective" in gold (`#FFC83D`) — never split with a space/hyphen or written as two words in copy.

Each of the five static pages (`index.html`, `equipment.html`, `home.html`, `admin.html`, `guides.html`) carries its own `<title>` — not shared, update each separately if the name ever changes again. The wordmark itself:
- `static/index.html`: two class fields near the top of `RinkFinder`, `BRAND_INK = 'Rink'` / `BRAND_GOLD = 'Collective'` (plus `BRAND` = their concatenation, used anywhere the plain name is needed in copy). `init()` renders `#nav-wordmark` from `BRAND_INK`/`BRAND_GOLD` directly.
- `static/home.html` and `static/guides.html`: same `BRAND_INK`/`BRAND_GOLD` pattern as top-level `const`s near the top of each page's own `<script>` (not shared between pages — each is standalone).
- `static/equipment.html`: hardcoded wordmark markup in the nav (no JS-driven split) — standalone page, not templated.

The rink-diagram mark (cyan rounded-rect boards, gold center line/face-off circle/dot) is inlined as raw SVG next to the wordmark in the nav of `index.html`, `equipment.html`, `home.html`, and `guides.html` (nav bar; `home.html`/`guides.html`'s mobile menu drawer only repeats the text wordmark, not a second copy of the SVG mark) — there's no shared partial, so a mark update means touching all of those plus `static/logo/` (`favicon.svg`, `mark-dark.svg`/`mark-light.svg`/`mark-mono.svg`, and the `favicon-{16,32,180,512}.png` rasters, regenerated to match since this repo has no SVG-to-PNG rasterizer as a dependency — see git history for the one-off Pillow script used).

---

## Architecture

### File Structure

```
barnbiscuit/
├── main.py                    # FastAPI app + SQLModel models
├── rinks.json                 # Curated rink data — source of truth, synced into the DB on startup
├── rinks_import_template.csv  # CSV template for bulk-adding rinks (fill in, run import script)
├── equipment.json             # Curated gear catalog data — source of truth, synced into the DB on startup
├── guides.json                 # Guides how-to library content — source of truth, synced into the DB on startup
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
    ├── home.html            # Standalone page — evergreen marketing/landing page, served at `/` (see Home Page section below)
    ├── index.html           # Entire frontend SPA (Rink Finder), served at `/rinks`
    ├── equipment.html       # Standalone page — gear catalog/compare/detail-drawer app (affiliate shopping, see Equipment section below)
    ├── admin.html           # Standalone page — full Admin Console (Overview/Photo Queue/Users/Rinks/Activity Log), served at `/admin`, gated by ADMIN_EMAILS (see Admin Console section below)
    ├── rink-admin.html      # Standalone page — Rink Owner Console (Schedule/Programs/Announcements), served at `/rink-admin`, gated per-rink by the RinkAdmin table (see Rink Owner Console section below)
    ├── guides.html          # Standalone page — how-to library + beginner-path checklist, served at `/guides` (see Guides section below)
    ├── brand-tokens.css     # CSS custom properties (Neon Night palette)
    └── logo/                # Favicons + SVG marks
```

### Backend (`main.py`)

Data is stored in a database (Postgres in production via Railway addon, local SQLite fallback otherwise) accessed through SQLModel. `rinks.json` remains the human/AI-edited source of truth — on every startup, `SQLModel.metadata.create_all()` creates tables if missing, `ensure_new_columns()` `ALTER TABLE ADD COLUMN`s anything a model has that its *already-existing* table doesn't (since `create_all()` only creates missing tables, not missing columns — without this, adding a field to any model crashes startup against a live Postgres table with `UndefinedColumn`; it loops over every table in its list, so new columns on any of them are covered — remember to add a new model to that list when it ships), then `sync_rinks_from_file()`, `sync_equipment_from_file()`, and `sync_guides_from_file()` each upsert (by `id`) every row from their JSON file into the matching table and delete any row whose `id` is no longer in the file. Pushing an updated `rinks.json`/`equipment.json`/`guides.json` to `main` (additions, edits, *and* removals) is enough to update production data on the next deploy — and adding a new column to `Rink`/`Equipment`/`Guide` itself is safe to deploy directly, no manual migration step needed. `EquipmentOffer`/`EquipmentPriceSnapshot` (see Equipment section below), `GuideProgress` (see Guides section below), and every table behind the Rink Owner Console (see that section below) are the exceptions to this file-is-truth pattern — they hold data no JSON file owns.

- `GET /` → serves `static/home.html`, the evergreen landing page (see Home Page section below)
- `GET /rinks` → serves `static/index.html`, the `RinkFinder` SPA (moved off `/` when Home shipped, so the app's own internal links — nav, "Explore rinks", deep links from Home — all point at `/rinks` now)
- `GET /api/rinks` → queries the `Rink` table, returns all rows as JSON (same shape as before)
- `GET /equipment` → serves `static/equipment.html`, the gear catalog page (not part of the `RinkFinder` SPA — same standalone-page pattern as `/admin`)
- `GET /api/equipment` → queries the `Equipment` table, and for each product merges in any matching `EquipmentOffer` rows via `serialize_equipment()` — if a product has zero offers (the common case today), it's served exactly as stored in `equipment.json`; if it has one or more live offers, `retailers`/`priceHistory`/`wasPrice`/`priceIsGood`/`deal`/`note` are computed from those instead, overriding the mock values without mutating them. The catalog is small (~37 rows) so all data ships at once; filtering/sorting/comparing happens client-side, same approach `RinkFinder` uses for the much larger rinks list
- `GET /guides` → serves `static/guides.html`, the Guides how-to library (not part of the `RinkFinder` SPA — same standalone-page pattern as `/equipment`; see Guides section below)
- `GET /api/guides` → queries the `Guide` table, returns all rows as JSON (no live-overlay serializer — content has no "live pricing" analog)
- `GET /api/guides/progress` → returns the signed-in user's beginner-path completion as `{guideId: true, ...}`; returns `{}` (not a 401) when signed out, matching `GET /api/auth/me`'s graceful signed-out shape rather than the hard-401 write-route pattern
- `POST /api/guides/progress/{guide_id}` → body `{completed: bool}`; upserts a `GuideProgress` row for the signed-in user (401 if signed out)
- `GET /api/photos/{rink_id}/{photo_idx}` → looks up `rink.photos[photo_idx].ref` (a Google Places photo resource name) and redirects to a freshly-fetched Google-hosted image URL, with a `Cache-Control` header so browsers don't re-hit this (and therefore Google) on every load. Requires `GOOGLE_PLACES_API_KEY`; 404s otherwise or if the rink/index doesn't exist, which the frontend treats as "no real photo" and falls back to placeholders
- `POST /api/rinks/submit` → inserts community-submitted rinks into the `PendingRink` table (`id`, `submittedAt`, raw `data` JSON blob) — not public until moderated, no validation yet
- `POST /api/auth/signup`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` → email/password auth against the `User` table (see Account & Onboarding section below for the full field list). Login state is a signed, httponly session cookie (Starlette `SessionMiddleware`, see `SECRET_KEY` above) holding `user_id` — no tokens handled in JS. `user_public()` (the shape all three return, plus the onboarding/avatar endpoints below) includes `isAdmin` (`email in ADMIN_EMAILS`) so every page's nav can show/hide its "Admin Console" link without any page needing its own copy of `ADMIN_EMAILS` — the actual admin endpoints are still independently gated server-side by `require_admin()`, this flag is display-only.
- `GET /api/auth/google/login`, `GET /api/auth/google/callback` → the "Continue with Google" flow (see Account & Onboarding section below and `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` above).
- `PATCH /api/auth/onboarding` → saves the post-signup onboarding wizard's progress incrementally (home rink/skill level, then interests/completion) — see Account & Onboarding section below.
- `POST /api/auth/avatar`, `GET /api/users/{user_id}/avatar` → real avatar upload/serving — see Account & Onboarding section below.
- `POST /api/rinks/{rink_id}/photos` → real user photo upload (multipart `file` + optional `caption`), requires sign-in (401 otherwise). Bytes are stored directly in the `RinkPhoto` table (`id`, `rinkId`, `userId`, `data` as `LargeBinary`, `contentType`, `caption`, `status` (`"pending"` → `"approved"`), `submittedAt`) — no external storage/CDN, same DB as everything else. Validates the rink exists (404), caps uploads at 8MB (413), and sniffs magic bytes to confirm real JPEG/PNG regardless of the client-supplied content type (400 otherwise). Every upload lands as `status="pending"` and is invisible everywhere until an admin approves it.
- `GET /api/rinks/{rink_id}/photos` → public list of that rink's `status="approved"` `RinkPhoto` rows (`[{id, caption}]`) — what the frontend merges into a rink's photo gallery, see `getPhotos()` below
- `GET /api/user-photos/{photo_id}` → serves the raw bytes of an approved photo (404 if not approved/doesn't exist), with the same `Cache-Control: public, max-age=3600` treatment as the Google photo proxy above
- `GET /admin` → serves `static/admin.html`, the full Admin Console (not part of the `RinkFinder` SPA — see Admin Console section below). `GET /admin/photos` redirects (307) here with `?view=photos`, kept only for bookmark compatibility with the console's pre-rewrite, photos-only form.
- `GET /api/admin/overview` → one combined payload for the Overview section: pending-photo count, total members + real 30-day signup delta, total rinks (no fabricated delta — there's no rink `createdAt` to compute one honestly), the signed-in admin's action count for today, up to 4 pending photos, and the 6 most recent `AdminActivity` rows.
- `GET /api/admin/photos?status=pending|approved`, `GET /api/admin/photos/{photo_id}/image`, `POST /api/admin/photos/{photo_id}/approve`, `POST /api/admin/photos/{photo_id}/reject` → list (with `{pending, approved}` counts)/view/approve/reject `RinkPhoto` rows, gated by `require_admin()` (see `ADMIN_EMAILS` above). Reject still hard-deletes the row (as before) but now reads a JSON body `{reason}` and folds it into the `AdminActivity` text, since there's no retained row left to attach a reason to.
- `GET /api/admin/users?q=&page=`, `GET /api/admin/rinks?q=&page=`, `GET /api/admin/rinks/{rink_id}`, `PATCH /api/admin/rinks/{rink_id}`, `GET /api/admin/activity?limit=` → the Users/Rinks/Activity Log sections, all `require_admin()`-gated and paginated (50/page) server-side. The `PATCH` sets `Rink.adminEditedAt` — see Admin Console section below for why.
- `GET /api/admin/rinks/{rink_id}/admins`, `POST .../admins`, `DELETE .../admins/{admin_id}` → `require_admin()`-gated grant/revoke of `RinkAdmin` access for a rink, by email (404 if that email hasn't signed up yet, 409 on a duplicate grant) — see Rink Owner Console section below.
- `GET /rink-admin` → serves `static/rink-admin.html`, the Rink Owner Console (not part of the `RinkFinder` SPA — see Rink Owner Console section below).
- `GET /api/rink-admin/rinks`, and the Schedule/Programs/Announcements CRUD endpoints under `/api/rink-admin/{rink_id}/...` → all gated by `require_rink_admin()` (401 signed out, 403 if the signed-in user has no `RinkAdmin` grant for that rink and isn't a superadmin) — see Rink Owner Console section below for the full endpoint list.
- `GET /api/rinks/{rink_id}/schedule`, `GET /api/rinks/{rink_id}/programs`, `GET /api/rinks/{rink_id}/announcements` → public, unauthenticated read-only feeds of a rink's console-managed schedule/programs/announcements, consumed by `static/index.html`'s rink drawer — see Rink Owner Console section below.
- `/static` → static file mount for CSS, logos, etc.

### Frontend (`static/index.html`)

Vanilla JS SPA — no bundler, no framework.

**`RinkFinder` class** manages all state and rendering:
- `this.rinks` — fetched from `/api/rinks` on init
- `this.state` — single state object (search, filters, selectedRinkId, drawerOpen, activeTab, locationStatus, mobileView, checkinsById, checkinConfirm, heroIdx, myCheckins, myReviews, communityPhotos, reviewOpen, reviewRating, reviewToast, photoToast, currentUser, accountMenuOpen, showAuth, authMode, authContext, showAddPhoto, photoDraftFile, photoPreviewUrl, photoUploadError, showOnboarding, onboardingStep, onboardingHomeRinkId, onboardingSkillLevel, onboardingInterests, onboardingSaving, onboardingError, onboardingToast, onboardingAvatarPreviewUrl, onboardingAvatarUploading, etc. — see Account & Onboarding section below for the auth/onboarding-specific ones)
- `setState(partial | fn)` — merges partial state and calls `render(prev)`
- `render(prev)` — diffs against prev state, updates the DOM in targeted sections

**Three dynamic render sections** (rebuilt via `innerHTML` on change):
- `#rink-list` + `#mobile-rink-list` — rink cards, rebuilt on filter/search/selection changes
- `#drawer-body` — Info/Photos/Reviews/Schedule/Programs tab content, rebuilt on selection/tab/checkin changes. Schedule (`renderEventsTab()`) and Programs (`renderProgramsTab()`) now render real console-managed data fetched from `GET /api/rinks/{id}/schedule` / `/programs` when a rink has any (see Rink Owner Console section below); a rink with none falls back to the legacy `rinks.json` `events` list for Schedule (Programs simply shows an empty state, since there's no equivalent legacy mock for it). Programs is read-only — it links skaters to "Contact the rink to register" rather than a self-serve signup/checkout flow.
- Modals — toggled via `display` on `showReport`/`showAddRink`/`showAuth`/`showAddPhoto`/`showOnboarding` state. The auth modal doubles as sign-in/sign-up, switching via `authMode` (`updateAuthUI()` toggles the first/last/display-name fields, the consent checkbox, the password-strength meter, title, and error text — see Account & Onboarding section below). The Add Photo modal (`renderAddPhotoModal()`) has an empty state (dropzone — drag/drop or browse, JPG/PNG only, wired via inline `ondragover`/`ondragleave`/`ondrop` rather than `addEventListener` since the node is recreated by `innerHTML` each time) and a filled state (object-URL preview via `photoPreviewUrl`, Remove button, optional caption) once a file is chosen via `handlePhotoFile()`. The post-signup onboarding modal (`renderOnboardingModal()`, `#onboarding-dynamic`) follows the same dynamic-innerHTML pattern — see Account & Onboarding section below.
- **Account menu** (`#nav-account-wrap`) — clicking the avatar while signed out opens the auth modal (`onAvatarClick`); while signed in it opens a small dropdown (`accountMenuOpen`) instead of logging out immediately, showing the display name, an "Admin Console" link to `/admin` (only if `currentUser.isAdmin`), and a "Log out" action. Closes on outside click or `Esc` (bound once in `bindEvents()`) — the toggle's own click handler calls `stopPropagation()` so it doesn't immediately re-trigger that same outside-click listener. This exact pattern (markup, ids, behavior) is duplicated — not shared — across `equipment.html`/`home.html`/`guides.html` too, same convention as the wordmark/mark duplication below; `equipment.html`'s copy rebuilds `#nav-account-wrap` via `innerHTML` each render (matching that page's blind-rebuild convention) rather than index.html's targeted-update style, which is *why* the `stopPropagation()` call is load-bearing there specifically — without it, the click's own DOM-replacement makes the outside-click check see a stale detached node and immediately close the menu it just opened. **The avatar itself is now real** (previously a decorative generic-person icon always) — `#nav-avatar`'s `innerHTML` is swapped for `<img src="{currentUser.avatarUrl}">` when the signed-in user has uploaded one, else a single initial-letter fallback; same swap, independently implemented (not shared), in all four pages — see Account & Onboarding section below.

**All other DOM updates** (location label, toggle state, filter chip active class, distance label, count) are targeted property sets, not full re-renders.

**Detail drawer structure** (`renderDrawer()`, `static/index.html`):
- Fixed **photo hero** (168px, gradient scrim) with a "{N} photos" / "No photos yet" chip and close button, overlaid rink name/badges — `getPhotos()` merges Google-sourced photos from `rink.photos` (served via the `/api/photos` proxy, with a small attribution caption) with approved community uploads from `state.communityPhotos[rink.id]` (served via `/api/user-photos/{id}` — fetched from `GET /api/rinks/{id}/photos` when a rink is selected, see `selectRink()`); only when *both* are empty does it fall back to `rinkPlaceholder(seed)`, a branded top-down rink-diagram SVG generated inline (no network request) and seeded by `rink.id` so a rink's set of `BASE_PHOTO_COUNT` (4) tiles — and each rink's placeholder set relative to others — reads as visually distinct (accent color + puck side vary by seed). These placeholder entries are tagged `placeholder: true`, which the drawer uses to: show the hero at full opacity instead of 0.85, swap the chip/footnote copy to the "no photos yet" variant, and hide the thumb rail's "+N" overflow tile (only shown for real photo counts). The thumb rail's and Photos tab's "Add" tiles call `openAddPhotoModal()`, which gates on `currentUser` (opens sign-in instead if signed out) — a real upload goes through `POST /api/rinks/{rink_id}/photos` and lands in a moderation queue (see Backend above), so `submitAddPhoto()` does *not* append it to the gallery or feature it as the hero; it just closes the modal, switches to the Photos tab, and shows a "submitted for review" toast (`photoToast`) so the confirmation is visible regardless of which tab the upload started from
- Below the hero, one scrollable container holds, in order: **thumb rail** (`renderThumbRail()` — click a thumb or the Photos tab to re-feature it as the hero via `heroIdx`), a **check-in + Directions row** (`renderCheckinRow()` — visible across all tabs, unlike the old Info-tab-only button), a **live check-in feed card** (`renderFeedCard()` — deterministic mock rows from `getMockFeed()`, plus a persistent "You" row once `myCheckins[rinkId]` is set), then a **sticky tab bar** (Info/Photos/Reviews/Schedule/Programs) and the tab body
- `myCheckins`/`myReviews` are session-only client state overlaid on top of the persisted `rinks.json` data (same pattern as the pre-existing `checkinsById`) — nothing here is sent to the backend. Photos are the exception: uploads are real and server-persisted (see Backend above), just not visible until approved
- The Reviews tab's composer reads its `<textarea>` via `document.getElementById('review-text').value` only at submit time (not mirrored into `state` on every keystroke) to avoid `innerHTML`-driven focus loss, since `renderDrawer()` is not diffed/keyed
- The Info tab shows any of that rink's **pinned** `Announcement` rows as colored banners above the address block (fetched via `GET /api/rinks/{id}/announcements` on `selectRink()`, same lazy-fetch-once-per-rink pattern as `communityPhotos`/`rinkSchedules`) — unpinned announcements aren't surfaced anywhere on the public page yet, only in the Rink Owner Console's own feed

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

**Deep links from Home** — `init()` reads `?auth=login|signup`, `?rink=<id>`, `?onboarding=1`, and `?error=google_unavailable` off the URL on load (after `this.rinks` is fetched), calls `showAuth()`/`selectRink()`/`startOnboarding()` accordingly, then strips them via `history.replaceState(null, '', '/rinks')` so a refresh doesn't re-trigger them. This is how `static/home.html`'s Sign in/Create account links and Featured Rinks/hero-carousel rink links reuse the real auth modal and drawer instead of duplicating either — and how the Google Sign-In redirect (a full page navigation, not an XHR) hands back into the same onboarding flow a normal signup uses (see Account & Onboarding section below).

### Account & Onboarding (`User` table, `static/index.html`'s auth/onboarding modals)

Signup collects more than email/password now, and a short post-signup wizard fills in the rest. Built in two passes — profile fields + onboarding first, then Google Sign-In/avatar/consent/password-meter second — both landed the same way the rest of this app is built: plain endpoints, vanilla-JS modals, no new frontend dependency.

- **`User` table fields** (beyond `id`/`email`/`passwordHash`/`displayName`/`createdAt`/`loginCount`): `firstName`, `lastName` (collected at signup, **never returned by any public-facing surface** — only `user_public()` reads them, and it's called exclusively by signup/login/me/onboarding/avatar, i.e. always describing the caller to themselves, never another user), `authProvider` (`"password"` | `"google"`), `consentAcceptedAt`, `homeRinkId`, `skillLevel` (`"new"|"rec"|"comp"|"coach"`, validated in the endpoint not the DB), `interests` (JSON list), `onboardingCompletedAt`, `avatarData`/`avatarContentType` (`LargeBinary`, same storage shape as `RinkPhoto.data`). `displayName` remains the **only public identity token** — there's no separate `@handle`, and it isn't enforced unique (see Not Yet Implemented below).
- **Signup** (`POST /api/auth/signup`) now requires `firstName` and a `consent: true` flag (stores `consentAcceptedAt`), and validates `displayName` length (2-30 chars). The client live-suggests `displayName` as "First L." while typing first/last name (plain DOM listeners in `bindEvents()`, not routed through `state` — same convention as the password-strength meter below), remaining editable; the server falls back to the same "First L." rule itself only if a JS-disabled client submits with `displayName` empty.
- **Password-strength meter** — advisory only, 3-segment bar under the password field, updated via a plain `input` listener; does not change the 8-char hard minimum the server already enforced pre-Phase-3.
- **Onboarding wizard** (`renderOnboardingModal()`, `#modal-onboarding`) — 2 steps, runs once immediately after a **new** signup only (email/password via `submitAuth()`, or Google via the `?onboarding=1` redirect param — see below), never replayed on login. Step 1: a client-side search over the already-fetched `this.rinks` list (no new endpoint) to set `homeRinkId`, plus a 4-card `skillLevel` picker. Step 2: an `interests` multi-select chip grid, plus an avatar dropzone (see below). Both steps are skippable; each step PATCHes `/api/auth/onboarding` immediately (body keys present = fields to update; `completed: true` on Step 2 or Skip stamps `onboardingCompletedAt`) so progress is never lost. The rink-search input deliberately does **not** mirror its value into `state` on every keystroke — same reasoning as the Reviews composer's `<textarea>` above (would re-render the whole modal via `innerHTML` per character and drop cursor focus); `filterOnboardingRinks()` does a targeted update of a results `<div>` instead. A page-level toast (`#toast-onboarding`, ~3s) confirms completion — a deliberate one-off exception to the tab-scoped `photoToast`/`reviewToast` pattern, since no drawer/tab is necessarily open when onboarding finishes.
- **Avatar upload** — `POST /api/auth/avatar` (multipart, reuses the `MAX_PHOTO_BYTES`/`PHOTO_SIGNATURES` constants `upload_rink_photo` already validates against) stores bytes directly on the signed-in user's row; `GET /api/users/{user_id}/avatar` serves them (`Cache-Control: public, max-age=3600`, same as `/api/user-photos/{photo_id}`). **No moderation queue**, unlike `RinkPhoto` — an avatar only ever displays next to its own uploader's name, the same self-attribution trust model `displayName` itself already has. `user_public()` computes `avatarUrl` from `avatarData` presence rather than storing a URL directly. The onboarding Step 2 dropzone (`handleAvatarFile()`) uploads immediately on file selection, no separate confirm step.
- **Google Sign-In** — `GET /api/auth/google/login` / `GET /api/auth/google/callback`, a hand-rolled OAuth2 authorization-code flow over `httpx` (already a dependency — no `authlib` or similar added). CSRF `state` round-trips through the session cookie. On callback, finds-or-creates a `User` by lowercased email; a new account gets `authProvider="google"` and an unusable random `passwordHash` (`hash_password(secrets.token_urlsafe(32))`) so it can only ever sign in via Google, never the password form. Redirects to `/rinks?onboarding=1` for a new account (picked up by `init()`'s deep-link parsing, since this is a full-page redirect, not an XHR that could call `submitAuth()`'s own onboarding trigger) or plain `/rinks` for a returning one. See `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` above for the graceful-degrade behavior when unconfigured.
- **Access gate context copy** — `showAuth(mode, context)` (`context`: `'photo'|'review'|'register'`) shows scenario-specific subtitle copy in the shared auth modal rather than a separate component. Wired from `openAddPhotoModal()` and `openReview()` (the latter is a new gate — "Write a Review" had no auth check at all before this). `'register'` copy exists but has no call site yet — there's no self-serve program-registration UI anywhere in this codebase to attach it to (see Rink Owner Console section below, Programs is owner-managed-roster only).
- **Nav avatar across all four pages** — `index.html`'s `#nav-avatar` and the equivalent element in `equipment.html`/`home.html`/`guides.html` each independently swap in `<img src="{avatarUrl}">` or an initial-letter fallback when `currentUser` is set — not shared code, same duplication convention as the rest of the account-menu markup (see Frontend section above).

### Home Page (`static/home.html`)

The site's front door, served at `GET /` — evergreen landing page for both first-time visitors and returning regulars, built from the "Barn & Biscuit — Home" design handoff (`design_handoff_home/` package). Standalone page (own top-level `class Home`, own `<script>`, same pattern as `equipment.html`), sharing only the nav bar visual language and brand tokens with Rink Finder/Equipment — not part of the `RinkFinder` SPA.

**Deliberately evergreen, no personalization yet** — everything on the page is either static copy or derived from data already in the `Rink` table; there is no feed/check-in/events pipeline behind it (matches the design doc's stated intent). Rink Finder itself moved off `/` to `/rinks` when this shipped — see Backend above.

- **Hero carousel** — auto-advances every 3.5s through the 5 highest-rated rinks (sorted by `rating` desc, `reviewCount` desc as tiebreaker, computed client-side from `/api/rinks` — no new backend endpoint). Two stacked `.hero-layer` divs cross-fade via an `active` class + CSS opacity transition (`showHero()`) rather than animating `background-image` directly (not something CSS can tween). Pager dots jump to a rink and permanently stop autoplay (`autoplayStopped`); hovering/focusing the carousel pauses autoplay temporarily (resumes on leave/blur, unless already stopped); `prefers-reduced-motion` disables autoplay entirely from the start. Images use a rink's real photo (`/api/photos/{id}/0`) when `rink.photos` is populated, falling back to the same `rinkPlaceholder(seed)` branded SVG generator used in `index.html` (copied in, not shared — standalone page).
- **Featured rinks** — reuses the same ranked list, taking the *next* 4 rinks after the hero's top 5 (`ranked.slice(5, 9)`) so the two sections don't just repeat each other; each row links to `/rinks?rink={id}` to deep-link straight into that rink's drawer in Rink Finder (see Deep links from Home above).
- **Rink counts** (hero eyebrow pill, trust row) are computed from the live `/api/rinks` count, floored to the nearest 50 (e.g. "650+ rinks"), rather than hardcoding the design mock's "400+" — avoids the copy silently going stale as `rinks.json` grows.
- **Guides nav item + "New here? Start with these" strip** — real now: the nav item links to `/guides` (same on `index.html`/`equipment.html`), and the strip (`renderGuides()`) fetches `/api/guides` and shows the first 4 guides in beginner-path order (`GUIDE_PATH_IDS`, kept in sync with `guides.html`'s `PATH_IDS`), each linking to `/guides?slug=<id>` with a topic-tinted thumbnail (`guideArt()` — see Guides section above). "All guides →" links to `/guides`.
- **"Latest reads" (News) section — intentionally omitted for now**, not just stubbed. The design doc explicitly allows hiding this section until ≥1 real article exists ("falls back gracefully"); since there's no News/CMS backend in this codebase at all, showing fabricated headlines/read-times would misrepresent real content. Featured Rinks (see above) fills the slot alone in that row until News is real. The dead `News` nav link (present in the design mockups but with no route behind it) was removed from all four pages' nav bars and mobile menu drawers — unlike Community below, it isn't expected to come back as a stub; it was never intentional in the first place.
- **Sign in / Create account** — not duplicated here; both link into Rink Finder's existing auth modal via `?auth=login`/`?auth=signup` query params (see Deep links from Home above). **Now reflects real session state** (a `home.html`-only regression from v1's "pre-personalization" launch, fixed since): `init()` fetches `/api/auth/me` alongside `rinks`/`guides` and swaps the signed-out links for the same account-menu dropdown described in the `index.html` Frontend section above (name, "Admin Console" if `isAdmin`, "Log out") — ported near-verbatim, not shared. This is also the site's only nav-based entry point into `/admin` for signed-in admins, alongside the same dropdown on every other page.
- **Mobile menu drawer** (`#menu-overlay`/`#menu-panel`, ≤768px) — slide-in from the right, opened by the hamburger button. Implements the production to-dos the design doc flagged as unwired in the prototype: closes on `Esc`, locks `body` scroll while open, and moves focus to the close button on open / hamburger on close (a lightweight trap, not a full focus cycle).

### Guides (`static/guides.html`, `guides.json`)

How-to library aimed at hockey beginners, built from the `design_handoff_guides/` design package. Standalone page at `GET /guides`, sharing only the nav bar and brand tokens with the other pages; own `class Guides { state, setState(partial), render() }` (same blind-full-rebuild pattern as `Equipment`, not `Home`'s ad-hoc-instance-property style), since it needs comparable multi-view/filter state.

- **Three views in one page** (`landing`/`category`/`article` in `this.state.view`, no client-side router): landing has a search-filtered all-guides grid, 5 topic cards, and the **beginner-path checklist** (5 fixed guide ids in editorial order, `PATH_IDS`); category shows one topic's guides with topic-chip switching; article shows the full reading view. Deep-linked via `?slug=<id>` (article) / `?topic=<id>` (category) query params, consumed on init and stripped via `history.replaceState(null, '', '/guides')` — same convention as `index.html`'s `?auth=`/`?rink=` deep links.
- **Topics are hardcoded, not DB-backed** — 5 fixed topics (Getting Started/Skating/Gear/Rules/Games & Play) with accent colors live in a `TOPICS` JS constant in `guides.html` (and a name+rgb-only subset, `GUIDE_TOPIC_ACCENTS`, duplicated in `home.html` for its guide strip — see Home Page below). Only guide *content* is DB-backed.
- **Placeholder art** — `art(rgb, seed)` generates a topic-tinted branded rink-diagram SVG data URI, same generator shape as `rinkPlaceholder(seed)`/`guideArt(rgb, seed)` in `home.html` but taking an explicit accent instead of looking one up by seed. **Important:** the SVG's own attributes use single quotes, and `encodeURIComponent` doesn't escape `'` — the generator must `.replace(/'/g, '%27')` after encoding, or a raw `'` survives into the data URI and prematurely closes whichever `url('...')`/`url("...")` CSS wrapper embeds it (this broke silently — cards rendered with no visible art — until fixed; keep the escape if this function is ever copied elsewhere).
- **Unauthored guides render honestly, not faked** — of the 13 guides in `guides.json`, "Starting to Play Hockey 101" (`starting-to-play-hockey-101`), "Hockey Gear Checklist" (`hockey-equipment-guide`), "Essential Hockey Skills" (`essential-hockey-skills-for-beginners`), "The Basic Rules of Hockey Explained" (`basic-rules-of-hockey-explained`), and "Common Beginner Hockey Mistakes" (`common-beginner-hockey-mistakes`) have a populated `body`; the other 8 ship with `body: []`. The article view checks `body.length` and shows a "this guide is still being written" panel (no TOC, no fake content) instead of silently substituting another guide's text — a deliberate departure from the design prototype, which faked every article open as the 101 guide. **Authored guides are kept deliberately short (~500-600 words, matching the 101 guide's length)** — drafts that go deeper are trimmed to an overview and cross-linked via `related` to whichever dedicated guide already owns that subtopic (e.g. the skills and rules guides both point at more specific guides — stopping, balance, offside/icing, penalties — rather than duplicating that content inline), so depth lives in one place per topic instead of being copy-pasted across guides.
- **Beginner-path progress is real, account-backed** — `GuideProgress` (`id`, `userId`, `guideId`, `completed`, `updatedAt`) persists which steps a signed-in user has checked off, via `GET`/`POST /api/guides/progress[/{guide_id}]` above. Toggling while signed out redirects to `/rinks?auth=login` instead of a local auth modal (`guides.html` has none — same convention as `home.html`/`equipment.html`, which link into `index.html`'s auth modal rather than duplicating it). The top nav's own sign-in state (separate from the progress feature) uses the same account-menu dropdown as every other page — see the `index.html` Frontend section above.
- **Mobile drawer** — ported near-verbatim from `home.html`'s trap-lite/Esc/scroll-lock pattern (same CSS/JS shape); the sticky TOC hides at the 768px breakpoint, same as the rest of the article column collapsing to one column.

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

Full-screen gear catalog/comparison app — RinkCollective's second feature alongside Rink Finder, and its first revenue path (affiliate shopping links). Standalone page at `GET /equipment`, sharing only the nav bar and brand tokens with Rink Finder; it has its own `class Equipment { state, setState(), render() }` (same pattern as `RinkFinder`, not the thin `admin.html` template-string style) because its UI — filters, sort, a multi-select compare tray, a compare overlay, a detail drawer — is comparably complex.

- **State** (`this.state` in `static/equipment.html`): `products` (fetched from `/api/equipment` on init), `category`, `search`, `brands` (checked filters), `maxPrice`, `minRating`, `sort`, `compare` (selected product ids, max 4), `compareOpen`, `detailId`, `currentUser`, `accountMenuOpen`.
- **Nav account menu** — real now (previously this page had no auth wiring at all, not even a `/api/auth/me` check — the person icon in the nav was purely decorative). `init()` fetches `/api/auth/me` alongside `/api/equipment`; `renderNavAccount()` renders a `/rinks?auth=login` link when signed out, or the same account-menu dropdown (name, "Admin Console" if `isAdmin`, "Log out") as every other page when signed in — see the `index.html` Frontend section above for the shared pattern.
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

### Data (`guides.json`)

Source of truth for Guides content, same sync-on-startup treatment as `rinks.json`/`equipment.json` (see Backend above): upserted by `id` (a URL slug, unlike `Rink`/`Equipment`'s integer ids) into the `Guide` table, stale rows deleted. Topics are not stored here — they're a hardcoded 5-entry taxonomy in `guides.html` (see Guides section above).

**Schema** (mirrors the `Guide` SQLModel in `main.py`):
```json
{
  "id": "starting-to-play-hockey-101",
  "topic": "start",
  "title": "Starting to Play Hockey 101: The Complete Beginner's Guide",
  "blurb": "The whole journey from never-laced-up to your first stride, in five simple steps.",
  "level": "Beginner",
  "readTime": "3 min read",
  "seed": 1,
  "tocIntroLabel": "Is hockey for beginners?",
  "body": [
    { "type": "p", "text": "..." },
    { "type": "h2", "id": "sec-1", "text": "1. Get comfortable on the ice" },
    { "type": "list", "items": ["...", "..."] },
    { "type": "tip", "text": "..." },
    { "type": "warning", "text": "..." },
    { "type": "gear-callout", "eyebrow": "Gear checklist", "title": "...", "text": "...", "href": "/equipment" }
  ],
  "related": ["hockey-equipment-guide", "essential-hockey-skills-for-beginners", "common-beginner-hockey-mistakes"]
}
```
`body` block `type`s are limited to what the five authored guides actually use (`p`, `h2`, `list`, `tip`, `warning`, `gear-callout`) — no video/gallery block types yet. `list` renders a plain bulleted `<ul>` (`items`: array of strings) — added for the gear-checklist guide, which is inherently list-shaped; no nested/numbered-list variant exists yet. `tocIntroLabel` becomes the TOC's first entry (pointing at the top of the article); every other TOC entry is derived from `h2` blocks. **8 of the 13 guides ship with `body: []`** (and `related: []`) — real titles/blurbs, no authored reading content yet — and render as "coming soon" in the article view rather than faking content (see Guides section above). `readTime` is hand-set, not computed from word count — update it by eye (roughly 200-240 wpm) whenever a guide's body changes materially.

### Admin Console (`static/admin.html`)

The site owner's (Luke's) internal back office — a left-sidebar dashboard with five sections (Overview, Photo Queue, Users, Rinks, Activity Log), built from the `design_handoff_admin/` design package. Standalone page at `GET /admin`, gated by `require_admin()`/`ADMIN_EMAILS` (see above) — 401 if signed out, 403 if signed in as a non-admin. Own `class Admin { state, setState(partial), render() }` (same blind-full-rebuild pattern as `Equipment`/`Guides`), sharing only `brand-tokens.css` and the rink-mark SVG with the public pages — sidebar, topbar, tables, drawer, and modals are all new. **Dark-mode only** by design, like the rest of the brand system, so it doesn't toggle with `data-theme`.

- **Single 900px breakpoint**, ported faithfully from the design: sidebar becomes a slide-in drawer (hamburger in the topbar, same scrim/`Esc`/focus pattern as `home.html`/`guides.html`'s mobile menu), stat grid drops to 2-up, both tables drop their secondary columns, and the Rink Editor drawer goes full-width. This is what makes the console usable one-handed from a phone without any extra mobile-specific work.
- **Overview** — 4 stat cards (pending photos, total members + real 30-day signup delta, total rinks, the signed-in admin's actions today — all live via `GET /api/admin/overview`, not the mock counts the design handoff shipped with) + a pending-photos card + a recent-activity card.
- **Photo Queue** — reuses the existing `RinkPhoto` moderation flow (see Backend above), with tabs, thumbnails via `/api/admin/photos/{id}/image`, and Approve/Reject actions. **Only Pending/Approved tabs exist — the design's Rejected tab was dropped**, because `POST /api/admin/photos/{photo_id}/reject` still hard-deletes the row (an existing, deliberate decision, not something this feature changed); there's no retained row for a Rejected tab to list. The Reject modal still collects a reason (reason chips + textarea, confirm disabled until non-empty) — it's folded into the `AdminActivity` log text instead of being stored on a photo row.
- **Users** — search + server-paginated table (`GET /api/admin/users?q=&page=`, 50/page). `role` is computed at query time, not stored: `superadmin` if the email is in `ADMIN_EMAILS`, `contributor` if the user has ≥1 approved `RinkPhoto`, else `member`. `User.loginCount` (new column) increments on every signup and login, powering the Logins column.
- **Rinks** — search + server-paginated table (`GET /api/admin/rinks?q=&page=`) and a **Rink Editor drawer** (`GET`/`PATCH /api/admin/rinks/{id}`) for type/phone/address/hours/amenities, using the design's clone-into-`rinkDraft`/Cancel-discards/Save-commits editing model. Two deliberate departures from the design handoff, because the real data doesn't match its mocked shape:
  - **Hours are free-text inputs per day**, not the design's structured `{closed, from, to}` time pickers — `rinks.json` stores hours as arbitrary strings (`"5:30am–10pm"`, `"Varies"`, `"Call for hours"`), which a `from`/`to` pair can't represent. A "Closed" button per row just fills that day's field with the literal string `"Closed"`.
  - **Amenities are one free-text comma-separated field**, not the design's fixed 8-chip toggle list — real amenities data is far messier than that 8-item taxonomy (258+ distinct values across the rink set: sheet counts, league affiliations, facility descriptors), so a fixed toggle list would silently drop anything not in it on save.
  - The Rinks table's Hours column is a same-value-grouping summary (`hours_summary()` in `main.py`) — no time parsing (values aren't reliably parseable): `"Daily {x}"` if all 7 days match, `"Weekdays {x} · Weekends {y}"` if Mon–Fri agree and Sat–Sun agree, else `"Hours vary by day"`.
  - **Saving a rink here is durable across deploys.** `Rink` gained an `adminEditedAt` column; `PATCH /api/admin/rinks/{id}` sets it, and `sync_rinks_from_file()` now skips the file-driven overwrite for any rink that has it set (deletion — a rink removed from `rinks.json` entirely — still applies). Without this, `rinks.json`'s startup resync (see Backend above) would silently revert every console edit on the next deploy. `rinks.json` stays the source of truth only for rinks the console has never touched — the same kind of exception `EquipmentOffer`/`GuideProgress` already are to the file-is-truth pattern.
  - The Rink Editor drawer also has a **Rink Admins** section — list/add-by-email/remove `RinkAdmin` grants for that rink (see Rink Owner Console section below). This is where a superadmin actually delegates a rink to its owner; the endpoints are `require_admin()`-gated (superadmin-only), not `require_rink_admin()`-gated.
- **Activity Log** — a new `AdminActivity` table (`id`, `kind` (`"user" | "photo" | "reject" | "rink"`), `text`, `actorId`, `actorName`, `createdAt`), written server-side by `log_activity()` at photo approve/reject, rink save, new-user signup, and Rink Admin grant/revoke (`kind: "rink"`, same as rink edits — no separate kind was added for this). Backs both this section and the Overview "recent activity" card — nothing here is client-generated the way the design mock's activity feed was.
- **A scoped "Rink Admin" role now exists** (see Rink Owner Console section below) — the sidebar's disabled "Rink Admin roles · SOON" row from the original design has been removed now that the feature ships, delegated per-rink through the Rink Editor drawer above rather than through its own dedicated sidebar section. Superadmin (global, via `ADMIN_EMAILS`) remains a separate, more powerful tier — it can still edit any rink directly here, and can open any rink's Owner Console too.

### Rink Owner Console (`static/rink-admin.html`)

The rink-manager-facing counterpart to the Admin Console — lets a scoped `RinkAdmin` (not a global superadmin) self-serve their own rink's page: ice-time **Schedule**, **Programs** (lessons/camps/clinics with rosters and a stubbed payments card), and **Announcements**. Standalone page at `GET /rink-admin`, built from the `design_handoff_rink_owner_console/` package, reusing `admin.html`'s sidebar+topbar+content shell per that handoff's explicit instruction. Own `class RinkOwner { state, setState(partial), render() }` (same blind-full-rebuild pattern as `Admin`/`Equipment`/`Guides`).

- **Access control** — a new `RinkAdmin` table (`id`, `userId`, `rinkId`, `createdAt`) grants a user Owner Console access scoped to one rink (a rink can have multiple admins; a user can admin multiple rinks). `require_rink_admin(request, session, rink_id)` mirrors `require_admin()` (401 signed out) but 403s only if the user has no matching grant *and* isn't a superadmin — `ADMIN_EMAILS` accounts can always open any rink's console, for support/testing. Grants are managed exclusively from the existing Admin Console's Rink Editor drawer (see Admin Console section above), not from within the Owner Console itself — there's no self-serve "request access" flow yet.
- **Init / rink selection** — `init()` fetches `/api/auth/me` (redirects to `/rinks?auth=login` if signed out), then `GET /api/rink-admin/rinks` (the rinks the signed-in user administers). A `?rink=<id>` query param can target a specific rink directly (handy for a superadmin opening a rink they haven't granted themselves). Zero administered rinks → a plain "you don't manage any rinks yet" message; more than one → a small rink switcher in the sidebar (not part of the original single-rink design, added for correctness since nothing stops a user from being granted more than one rink).
- **Schedule module** — `IceSheet` (`id`, `rinkId`, `name`, `sortOrder`) and `IceSession` (`id`, `rinkId`, `sheetId`, `type`, `dayOfWeek`, `date`, `start`, `end`, `price`, `cap`, `ages`, `repeatWeekly`, `linkedProgramId`). Modeled as a **recurring weekly template**, not per-date instances: a `repeatWeekly=true` session has `dayOfWeek` set and appears on that weekday every week indefinitely; a one-time session has `date` set instead and appears only in that specific week. `type == "Learn to Skate"` auto-creates a linked draft `Program` (`linkedProgramId` on both sides), per the design's "Creates a linked program in the Program Manager" note. `POST .../sessions/copy-last-week` duplicates the *previous week's one-time* sessions forward 7 days (recurring sessions never need copying — they already repeat on their own). Endpoints: `GET/PUT /api/rink-admin/{rink_id}/sheets` (sheets are saved as one batch — add/rename/remove — cascade-deleting a removed sheet's sessions; at least one sheet is always required), `GET/POST /api/rink-admin/{rink_id}/sessions`, `DELETE .../sessions/{id}`, `POST .../sessions/copy-last-week`. **The "Publish changes" bar is cosmetic only** — every edit saves live immediately server-side; clicking Publish just clears the owner's own client-side unpublished-change counter, matching the design handoff's explicit note that its prototype "does not persist a real publish."
- **Programs module** — `Program` (`id`, `rinkId`, `name`, `type`, `startDate`, `endDate`, `ageRange`, `scheduleNote`, `price`, `cap`, `status`, `linkedSessionId`) and `ProgramRegistrant` (`id`, `programId`, `name`, `age`, `payStatus`, `waitlisted`) — registrants have no `userId`; rosters are entirely owner-managed rows (kids typically don't have their own RinkCollective accounts). Only `draft`/`open`/`closed`/`cancelled` are real stored statuses — **`waitlist` and `past` are computed on every read, never stored** (`effective_program_status()` in `main.py`): waitlist when `open` and enrolled ≥ cap, past once `endDate` has elapsed, so nothing needs a scheduled job to flip a program's status as time passes. Adding a registrant past capacity marks them `waitlisted=true`; removing a registrant (or raising `cap`) auto-promotes the oldest waitlisted registrant into an enrolled slot (`promote_waitlisted_registrants()`). Capacity can't be lowered below the current enrolled count (400 otherwise). **Payments are a Stripe UI stub only** — the "Payments · Stripe" card, revenue figure, and "Manage" button are display-only, no Stripe integration exists anywhere in this codebase (same unbuilt state as Equipment's checkout, but for program registration instead of gear purchases). "Message all" / "Export" / per-registrant "Remind" / "Message" are all toast-only acknowledgements with no backing SMS/email/CSV infra.
- **Announcements module** — `Announcement` (`id`, `rinkId`, `authorId`, `type` (`alert`/`update`/`event`), `text`, `audience`, `pinned`, `pushNotify`, `seenCount`). Take-down hard-deletes the row, same convention as `RinkPhoto` reject. `pushNotify` is stored but is a no-op — no push infrastructure exists. Public `GET /api/rinks/{rink_id}/announcements` increments `seenCount` on every fetch — an approximate counter in the same spirit as the existing `checkins`/`reviewCount` fields, not unique-visitor tracked.
- **Public surfacing** (`static/index.html`, see Frontend section above) — the rink drawer's Schedule tab renders real sessions from `GET /api/rinks/{rink_id}/schedule` when a rink has any, falling back to the legacy `rinks.json` `events` list otherwise; a new Programs tab shows `GET /api/rinks/{rink_id}/programs` read-only (name/dates/price/spots-left, no registrant PII) with a "Contact the rink to register" note rather than a self-serve signup/checkout flow, since real payment processing doesn't exist; pinned announcements render as banners at the top of the Info tab.
- None of `RinkAdmin`/`IceSheet`/`IceSession`/`Program`/`ProgramRegistrant`/`Announcement` are backed by a JSON file — they're pure database tables (same category as `EquipmentOffer`/`GuideProgress`), added to `ensure_new_columns()`'s table list alongside the rest.

### Brand System (`static/brand-tokens.css`, `static/logo/`)

- Copied from `C:\Users\lukec\Desktop\SpendTools\design_handoff_brand_system\` — do not edit in place; re-copy from source if the design system is updated
- Dark theme activated by `<html data-theme="dark">` on the root element
- All colors in `index.html` use `var(--token-name)` from this file
- Key tokens: `--bg` (#0A0E1A), `--surface` (#131A2B), `--surface-2` (#1C2540), `--border` (#2A3450), `--color-primary` (cyan #14CFCF), `--font-display` (Space Grotesk), `--font-body` (Hanken Grotesk), `--font-mono` (Space Mono)

---

## Not Yet Implemented

- Ongoing Google Places sync — the backfill (`scripts/fetch_google_places_data.py`) is a one-time pull, not a scheduled refresh; rinks added after a backfill run (or newly opened Google listings) need a manual re-run to pick up real ratings/reviews/photos, and unmatched rinks keep placeholder content indefinitely until then
- Admin UI for moderating community-submitted rinks (sit in the `PendingRink` table, unvalidated) — the Admin Console's Photo Queue (`/admin`, gated on `ADMIN_EMAILS`, see Admin Console section below) is a close precedent to extend for this: same `require_admin()` gate, same list/approve/reject shape, just against `PendingRink` instead of `RinkPhoto`, and a new sixth sidebar section rather than a tab within Photo Queue
- Server-persisted *user-submitted* check-ins and reviews (session-only in v1 — see `myCheckins`/`myReviews` above) — accounts now exist to attribute these to, but neither is wired to the `User` table yet. Photos got this treatment already (`POST /api/rinks/{rink_id}/photos` → `RinkPhoto` table → `/admin/photos` review queue → public once approved) and would be the template to follow. This is separate from the Google-sourced reviews/photos in `rinks.json`, which are real but were pulled once, not submitted by app users
- Real schema migrations (no Alembic) — `ensure_new_columns()` in `main.py` covers the common case of adding a new nullable column, but column renames/type changes/drops still have no automated path and would need a manual `ALTER TABLE` against the Railway Postgres addon
- Community section — **temporarily removed from the nav** (not deleted) in favor of Equipment, a business decision to prioritize the affiliate revenue path. The dead `<a>` link is commented out in `static/index.html`'s nav (`static/equipment.html` never had it); there's no actual Community route/component to restore beyond that. The `News` nav link, unlike this one, was removed outright (not commented out) from all four pages — it was a stray mockup artifact with no intended feature behind it, not a deliberate stub (see Home Page section above)
- Real affiliate program for Equipment — both automated networks are on hold (AvantLink denied the site's application for insufficient traffic/content; Amazon PA-API requires 10 sales in 30 days before granting API access), so real offers currently come in via manual entry (`scripts/add_manual_offers.py`, `network="manual"`) with plain, untagged retailer URLs. Buy/View links in `static/equipment.html` now navigate for real once a product has a live offer (see Live Offers above), but nothing is monetized yet — Sovrn Commerce (formerly VigLink), which needs no traffic review or per-retailer application, is the planned way to auto-monetize those existing links, but the site isn't signed up yet. The affiliate disclosure copy in the detail drawer is still placeholder text pending legal review regardless. Once the site has more real traffic/content (helped by Equipment itself actually working), reapply to AvantLink and revisit Amazon PA-API; other retailers/networks (HockeyMonkey via Rakuten/Pepperjam, Ice Warehouse via Awin) aren't evaluated yet. Matching the same physical product across multiple retailers/networks has no reliable automated path regardless of network — it needs the same hand-reviewed CSV approach these scripts already use.
- Equipment mobile layout — the catalog/sidebar/compare-table/drawer are desktop-only (matches the design handoff, which explicitly flagged mobile as an unscoped follow-up)
- "Submit an Event" button (UI only, no backend) — "Write a Review" now has a working session-local composer (see above), just not server-persisted. Only shown in the Schedule tab's legacy fallback rendering (a rink with no console-managed schedule yet, see Rink Owner Console section above) — rinks with a real schedule don't show this dead button at all
- Real Stripe payments for the Rink Owner Console's Programs module — registration checkout, payouts, and reconciliation are all unbuilt; the "Payments · Stripe" card is a UI stub only (see Rink Owner Console section above), same underlying gap as Equipment's affiliate checkout, just for program registration instead of gear purchases
- Real messaging/push infra for the Rink Owner Console — Announcements' `pushNotify` and Programs' "Remind"/"Message"/"Message all" actions are all toast-only acknowledgements with no SMS/email/push backend; "Export roster" is similarly a toast, not a real CSV download
- Self-serve Rink Admin requests — a rink owner can't request access themselves; a superadmin has to manually grant `RinkAdmin` by email per rink from `/admin`'s Rink Editor drawer (see Admin Console / Rink Owner Console sections above). There's also no single page listing every rink's admins across the whole site — grants are only visible/manageable one rink at a time
- Public self-serve Program registration — the public Programs tab (see Rink Owner Console section above) is read-only; real signups still happen off-platform and get entered into the roster by hand by the rink admin
- Guides content — 8 of the 13 guides in `guides.json` have real titles/blurbs but no authored `body` yet (see Guides section above for which 5 are written); they render as "coming soon" in the article view until content is added. The TOC's active-section highlight also only updates on click — no scroll-spy/`IntersectionObserver` yet (same to-do as the design handoff flagged).
- News / "Latest reads" — no CMS or News backend exists at all, so `static/home.html` omits that section entirely rather than showing fabricated headlines (see Home Page section above). Build once there's ≥1 real article to show
- Forgot-password flow — deliberately deferred; this codebase has no email-sending infrastructure anywhere (no SMTP/SendGrid/etc.), and building it against a guessed provider would be wasted/wrong-shaped work. Needs a real provider decision (generic SMTP via stdlib `smtplib`, vs. a transactional HTTP API like Resend) before it's built.
- Display-name uniqueness/profanity rules — an explicit open question in the original design doc for this feature, not resolved here: `displayName` is freely editable and **not** enforced unique, matching pre-existing behavior. Revisit if review/reporting moderation ever needs a stable, unique identifier.
- Google OAuth consent-screen production readiness — the OAuth client itself works (see Account & Onboarding section above), but Google's consent screen has its own **Publishing status** gate in Cloud Console, separate from the env vars: while it's "Testing," only explicitly-added test-user emails can complete sign-in at all, and going "In production" needs an app name/support email at minimum. There's also no real Privacy Policy/Terms page in this codebase yet — the consent checkbox added alongside this feature links to `#` placeholders (same "pending legal review" treatment as the Equipment section's affiliate disclosure copy) — so real users will likely see Google's "unverified app" warning interstitial until a real privacy page exists and the screen is verified.
- Split-panel visual redesign for the auth/onboarding screens — the design handoff for this feature specified a fancier split-panel layout with a live "how you'll show up" preview and community-proof imagery; deliberately not built, in favor of shipping the underlying data model and states first using the existing simple-modal system every other page already uses.
