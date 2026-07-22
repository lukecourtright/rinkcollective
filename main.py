import asyncio
import base64
import html
import json
import os
import pathlib
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import bcrypt
import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import JSON, Column, LargeBinary, func, inspect, text
from sqlalchemy.schema import CreateColumn
from sqlmodel import Field, Session, SQLModel, create_engine, select
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")

# Google Sign-In (Phase 3) — optional, same graceful-degrade convention as
# GOOGLE_PLACES_API_KEY: not required locally or in prod, the "Continue with
# Google" flow just redirects back with an error if unset. No redirect-URI
# env var — it's derived from the incoming request so local/prod both work
# with no extra config.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")

ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

# Email Campaigns (Admin Console) — same graceful-degrade convention as
# GOOGLE_PLACES_API_KEY: sending just no-ops (rows stay "queued") if unset,
# nothing crashes at startup. SITE_BASE_URL builds absolute tracking/
# unsubscribe links from the background worker, which (unlike a request
# handler) has no request.base_url to derive them from.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS", "Rink Collective <hello@rinkcollective.com>")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://rinkcollective.com").rstrip("/")

RINKS_FILE = pathlib.Path("rinks.json")
EQUIPMENT_FILE = pathlib.Path("equipment.json")
GUIDES_FILE = pathlib.Path("guides.json")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")
if DATABASE_URL.startswith("postgres://"):
    # Railway/Heroku-style URLs use "postgres://"; SQLAlchemy needs "postgresql://"
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)


class Rink(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str
    address: str
    city: str
    state: str
    lat: float
    lng: float
    type: str
    isPublic: bool
    rating: float = 0
    reviewCount: int = 0
    phone: Optional[str] = None
    website: Optional[str] = None
    checkins: int = 0
    hours: dict = Field(default_factory=dict, sa_column=Column(JSON))
    amenities: list = Field(default_factory=list, sa_column=Column(JSON))
    events: list = Field(default_factory=list, sa_column=Column(JSON))
    reviews: list = Field(default_factory=list, sa_column=Column(JSON))
    photos: list = Field(default_factory=list, sa_column=Column(JSON))
    googlePlaceId: Optional[str] = None
    adminEditedAt: Optional[str] = None
    adminOnly: Optional[bool] = None


class Equipment(SQLModel, table=True):
    id: int = Field(primary_key=True)
    category: str
    brand: str
    name: str
    rating: float = 0
    reviewCount: int = 0
    imageUrl: Optional[str] = None
    deal: Optional[str] = None
    note: str = "Stable price"
    priceIsGood: bool = False
    wasPrice: Optional[float] = None
    priceHistory: list = Field(default_factory=list, sa_column=Column(JSON))
    featuredQuote: str = ""
    retailers: list = Field(default_factory=list, sa_column=Column(JSON))
    specs: list = Field(default_factory=list, sa_column=Column(JSON))
    reviewList: list = Field(default_factory=list, sa_column=Column(JSON))


class EquipmentOffer(SQLModel, table=True):
    # One row per (product, retailer) live listing, populated/refreshed only
    # by scripts/fetch_amazon_products.py or scripts/fetch_avantlink_products.py
    # — never touched by the equipment.json startup sync, so a deploy can't
    # stomp a live price back to a stale mock value. See CLAUDE.md
    # "Equipment: live offers" section.
    id: Optional[int] = Field(default=None, primary_key=True)
    equipmentId: int
    retailerName: str
    network: str
    sourceProductId: str
    sourceMerchantId: Optional[str] = None  # network-assigned merchant/advertiser id, needed by some networks (e.g. AvantLink) to re-look-up a specific offer directly instead of re-searching by keyword
    price: float
    url: str
    inStock: bool = True
    lastCheckedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EquipmentPriceSnapshot(SQLModel, table=True):
    # One row per price check of a given EquipmentOffer over time, so the
    # site can show a real price history / "lowest in 90 days" instead of
    # only the current price.
    id: Optional[int] = Field(default=None, primary_key=True)
    equipmentOfferId: int
    price: float
    checkedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Guide(SQLModel, table=True):
    # Content for the Guides how-to library, upserted from guides.json on
    # startup (same pattern as Rink/Equipment). id is a URL slug rather than
    # an opaque int, since guides are routed by slug. Only the "101" guide
    # ships with a populated body/related today — the rest have body: [] and
    # render as "coming soon" until authored (see static/guides.html).
    id: str = Field(primary_key=True)
    topic: str
    title: str
    blurb: str
    level: str = "Beginner"
    readTime: str
    seed: int = 0
    tocIntroLabel: str = ""
    body: list = Field(default_factory=list, sa_column=Column(JSON))
    related: list = Field(default_factory=list, sa_column=Column(JSON))


class GuideProgress(SQLModel, table=True):
    # One row per (user, guide) beginner-path completion — powers the
    # persisted checklist on the Guides landing page.
    id: Optional[int] = Field(default=None, primary_key=True)
    userId: int
    guideId: str
    completed: bool = True
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PendingRink(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    submittedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict = Field(sa_column=Column(JSON))


class RinkPhoto(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rinkId: int
    userId: int
    data: bytes = Field(sa_column=Column(LargeBinary))
    contentType: str
    caption: Optional[str] = None
    status: str = "pending"
    submittedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    passwordHash: str
    displayName: str
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # server_default (not just the Python-side default=0) so
    # ensure_new_columns()'s ALTER TABLE ADD COLUMN can backfill existing
    # rows — a NOT NULL column added with no DB-level default fails against
    # a table that already has rows (as production's user table did).
    loginCount: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # server_default like loginCount above — NOT NULL on a table that
    # already has rows. "password" for every account created before Google
    # Sign-In shipped, "google" for accounts created via that flow (their
    # passwordHash is an unusable random value — see the OAuth callback).
    authProvider: str = Field(default="password", sa_column_kwargs={"server_default": "password"})

    # Private — collected at signup, never returned by any public-facing
    # surface. Only user_public() reads these, and it's called exclusively
    # by signup/login/me, i.e. it always describes the caller to themselves,
    # never another user (public surfaces read .displayName off the ORM
    # object directly instead). All nullable so ensure_new_columns() can
    # ALTER TABLE these onto existing rows with no backfill needed.
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    consentAcceptedAt: Optional[str] = None

    # Real avatar storage (Phase 3) — same LargeBinary-on-the-row pattern as
    # RinkPhoto.data, no separate table since it's 1:1 with the user. No
    # moderation queue: an avatar only ever displays next to its own
    # uploader's name, the same self-attribution trust model as displayName
    # itself already has. user_public() computes a serving URL from these
    # rather than storing one directly.
    avatarData: Optional[bytes] = Field(default=None, sa_column=Column(LargeBinary))
    avatarContentType: Optional[str] = None

    # Onboarding, collected progressively via PATCH /api/auth/onboarding.
    homeRinkId: Optional[int] = None
    skillLevel: Optional[str] = None  # "new" | "rec" | "comp" | "coach" — validated in the endpoint, not DB-enforced
    interests: Optional[list] = Field(default=None, sa_column=Column(JSON))
    onboardingCompletedAt: Optional[str] = None

    # Email Campaigns (nullable, same ensure_new_columns() backfill-free
    # convention as Rink.adminOnly). lastLoginAt powers the Active/Inactive
    # audience segment (set at signup/login/Google-callback, falls back to
    # createdAt for a "last seen" value if a user never logs in again);
    # emailUnsubscribedAt is the one-click, all-campaigns unsubscribe.
    lastLoginAt: Optional[str] = None
    emailUnsubscribedAt: Optional[str] = None


class RinkAdmin(SQLModel, table=True):
    # Grants a User "Rink Owner Console" access scoped to one rink. A rink
    # can have multiple admins and a user can admin multiple rinks. Granted
    # by a superadmin (ADMIN_EMAILS) from the Rink Editor drawer in the
    # existing Admin Console — see require_rink_admin() below.
    id: Optional[int] = Field(default=None, primary_key=True)
    userId: int
    rinkId: int
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class IceSheet(SQLModel, table=True):
    # One ice surface at a rink (e.g. "Rink A"), owned/named by the rink's
    # RinkAdmin(s) via the Rink Owner Console's Schedule module.
    id: Optional[int] = Field(default=None, primary_key=True)
    rinkId: int
    name: str
    sortOrder: int = 0


class IceSession(SQLModel, table=True):
    # A recurring weekly ice-time slot (dayOfWeek set, repeatWeekly=True) or
    # a single one-off slot (date set, repeatWeekly=False) on one IceSheet.
    # type == "Learn to Skate" also creates a linked Program (linkedProgramId).
    id: Optional[int] = Field(default=None, primary_key=True)
    rinkId: int
    sheetId: int
    type: str
    dayOfWeek: Optional[int] = None
    date: Optional[str] = None
    start: str
    end: str
    price: float = 0
    cap: int = 0
    ages: str = ""
    repeatWeekly: bool = True
    linkedProgramId: Optional[int] = None
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Program(SQLModel, table=True):
    # A lesson/camp/clinic run by a rink, managed via the Programs module of
    # the Rink Owner Console. scheduleNote/ageRange are free text (real
    # program schedules/age ranges don't fit a rigid picker — same rationale
    # as Rink hours/amenities in the existing Admin Console).
    id: Optional[int] = Field(default=None, primary_key=True)
    rinkId: int
    name: str
    type: str = ""
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    ageRange: str = ""
    scheduleNote: str = ""
    price: float = 0
    cap: int = 0
    status: str = "draft"
    linkedSessionId: Optional[int] = None
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ProgramRegistrant(SQLModel, table=True):
    # One roster row on a Program. No userId — rosters are owner-managed
    # (kids typically don't have their own RinkCollective accounts); message
    # /remind/cancel-refund actions are all owner-side.
    id: Optional[int] = Field(default=None, primary_key=True)
    programId: int
    name: str
    age: Optional[int] = None
    payStatus: str = "due"
    waitlisted: bool = False
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Announcement(SQLModel, table=True):
    # A rink news/alert/event post from the Announcements module. Take-down
    # hard-deletes the row, same convention as RinkPhoto reject.
    id: Optional[int] = Field(default=None, primary_key=True)
    rinkId: int
    authorId: int
    type: str = "update"
    text: str
    audience: str = "everyone"
    pinned: bool = False
    pushNotify: bool = False
    seenCount: int = 0
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AdminActivity(SQLModel, table=True):
    # Server-generated audit trail behind the Admin Console's Overview
    # "recent activity" card and the Activity Log section. kind drives icon
    # + color in the frontend: "user" | "photo" | "reject" | "rink" | "campaign".
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str
    text: str
    actorId: Optional[int] = None
    actorName: Optional[str] = None
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def log_activity(session: Session, kind: str, text: str, actor: Optional[User] = None):
    session.add(AdminActivity(
        kind=kind,
        text=text,
        actorId=actor.id if actor else None,
        actorName=actor.displayName if actor else None,
    ))


class EmailCampaign(SQLModel, table=True):
    # A one-time "broadcast" (subject/bodyHtml/audienceActivity set directly)
    # or the single "automation" container (just a named, toggleable shell —
    # its real content lives in EmailAutomationStep rows below). Only one
    # automation campaign ever exists (the Welcome Series, seeded once at
    # startup by seed_welcome_series()) — there's no generic automation
    # builder yet. Pure DB table, same category as EquipmentOffer/
    # GuideProgress — no JSON file owns this.
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # "broadcast" | "automation"
    name: str
    status: str = "draft"  # "draft" | "scheduled" | "sent" | "active" | "paused"
    subject: Optional[str] = None
    bodyHtml: Optional[str] = None
    # The one link counted as "Converted" in the report funnel — set from the
    # compose screen's "Insert button" CTA, not a fabricated stat.
    ctaUrl: Optional[str] = None
    audienceActivity: Optional[str] = None  # "active" | "inactive" | "all" — broadcast only
    scheduledAt: Optional[str] = None
    sentAt: Optional[str] = None
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    active: bool = True  # automation's on/off state ("dripOn" in the design)


class EmailAutomationStep(SQLModel, table=True):
    # One step of an automation's sequence (currently only the Welcome
    # Series exists), in `order`. waitDays=0 means "send immediately on entry".
    id: Optional[int] = Field(default=None, primary_key=True)
    campaignId: int
    order: int
    waitDays: int = 0
    subject: str
    bodyHtml: str


class EmailRecipient(SQLModel, table=True):
    # One row per (recipient, send) — doubles as the send queue (the
    # background worker drains status="queued" rows) and the tracking record
    # behind the report/funnel views. stepId is set only for automation-step
    # sends; a plain broadcast send leaves it null. isTest rows (from "Send
    # myself a test") are excluded from all campaign performance stats.
    id: Optional[int] = Field(default=None, primary_key=True)
    campaignId: int
    stepId: Optional[int] = None
    userId: int
    token: str = Field(index=True, unique=True)
    isTest: bool = False
    status: str = "queued"  # "queued" | "sent" | "failed"
    queuedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sentAt: Optional[str] = None
    openedAt: Optional[str] = None
    clickedAt: Optional[str] = None
    unsubscribedAt: Optional[str] = None
    resendMessageId: Optional[str] = None


class EmailClick(SQLModel, table=True):
    # One row per link click on a sent email; "Top links clicked" in the
    # report groups these by url.
    id: Optional[int] = Field(default=None, primary_key=True)
    recipientId: int
    url: str
    clickedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EmailAutomationEntry(SQLModel, table=True):
    # One row per (user, automation) — tracks progress through the sequence.
    # Created at signup (and in the Google callback's new-account branch)
    # when the automation is active; advanced by the background worker as
    # each step's waitDays elapses.
    id: Optional[int] = Field(default=None, primary_key=True)
    campaignId: int
    userId: int
    enteredAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    nextStepOrder: int = 1
    completedAt: Optional[str] = None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def suggest_display_name(first_name: str, last_name: str) -> str:
    # Shared by signup's defensive fallback and the Google callback (which
    # has no client-side JS to live-suggest one) — "First L." when a last
    # name is available, just "First" otherwise.
    return f"{first_name} {last_name[0]}." if last_name else first_name


def user_public(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.displayName,
        "firstName": user.firstName,
        "lastName": user.lastName,
        "avatarUrl": f"/api/users/{user.id}/avatar" if user.avatarData else None,
        "homeRinkId": user.homeRinkId,
        "skillLevel": user.skillLevel,
        "interests": user.interests or [],
        "onboardingCompletedAt": user.onboardingCompletedAt,
        "authProvider": user.authProvider,
        "isAdmin": user.email in ADMIN_EMAILS,
    }


def require_admin(request: Request, session: Session) -> User:
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in required")
    user = session.get(User, user_id)
    if user is None or user.email not in ADMIN_EMAILS:
        raise HTTPException(403, "Admin access required")
    return user


def require_rink_admin(request: Request, session: Session, rink_id: int) -> User:
    # Global admins (ADMIN_EMAILS) can access any rink's Owner Console for
    # support/testing; everyone else needs a matching RinkAdmin grant.
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in required")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "Sign in required")
    if user.email in ADMIN_EMAILS:
        return user
    grant = session.exec(
        select(RinkAdmin).where(RinkAdmin.userId == user_id, RinkAdmin.rinkId == rink_id)
    ).first()
    if grant is None:
        raise HTTPException(403, "You don't manage this rink")
    return user


def ensure_new_columns():
    # create_all() only creates missing tables, not missing columns on tables
    # that already exist in production — this adds any model column the live
    # table doesn't have yet, so schema additions don't need Alembic.
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table_name, model in (
        ("rink", Rink),
        ("equipment", Equipment),
        ("equipmentoffer", EquipmentOffer),
        ("equipmentpricesnapshot", EquipmentPriceSnapshot),
        ("guide", Guide),
        ("guideprogress", GuideProgress),
        ("user", User),
        ("adminactivity", AdminActivity),
        ("rinkadmin", RinkAdmin),
        ("icesheet", IceSheet),
        ("icesession", IceSession),
        ("program", Program),
        ("programregistrant", ProgramRegistrant),
        ("announcement", Announcement),
        ("emailcampaign", EmailCampaign),
        ("emailautomationstep", EmailAutomationStep),
        ("emailrecipient", EmailRecipient),
        ("emailclick", EmailClick),
        ("emailautomationentry", EmailAutomationEntry),
    ):
        if table_name not in existing_tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        # Quote the table name via the dialect's identifier preparer — "user"
        # is a reserved word in Postgres (unlike SQLite, which is lenient
        # about it), so a raw f-string ALTER TABLE user ... fails there.
        quoted_table = engine.dialect.identifier_preparer.quote(table_name)
        with engine.begin() as conn:
            for column in model.__table__.columns:
                if column.name not in existing:
                    ddl = CreateColumn(column).compile(dialect=engine.dialect)
                    conn.execute(text(f"ALTER TABLE {quoted_table} ADD COLUMN {ddl}"))


def sync_rinks_from_file():
    rinks = json.loads(RINKS_FILE.read_text(encoding="utf-8"))
    with Session(engine) as session:
        for rink in rinks:
            existing = session.get(Rink, rink["id"])
            if existing is not None and existing.adminEditedAt:
                # Rink was edited in the Admin Console — rinks.json no longer
                # owns its fields, so skip the overwrite (see CLAUDE.md "Live
                # Offers" precedent: EquipmentOffer/GuideProgress are also
                # exceptions to the file-is-truth pattern).
                continue
            session.merge(Rink(**rink))
        file_ids = {r["id"] for r in rinks}
        for stale in session.exec(select(Rink).where(Rink.id.not_in(file_ids))).all():
            session.delete(stale)
        session.commit()


def sync_equipment_from_file():
    products = json.loads(EQUIPMENT_FILE.read_text(encoding="utf-8"))
    with Session(engine) as session:
        for product in products:
            session.merge(Equipment(**product))
        file_ids = {p["id"] for p in products}
        for stale in session.exec(select(Equipment).where(Equipment.id.not_in(file_ids))).all():
            session.delete(stale)
        session.commit()


def sync_guides_from_file():
    guides = json.loads(GUIDES_FILE.read_text(encoding="utf-8"))
    with Session(engine) as session:
        for guide in guides:
            session.merge(Guide(**guide))
        file_ids = {g["id"] for g in guides}
        for stale in session.exec(select(Guide).where(Guide.id.not_in(file_ids))).all():
            session.delete(stale)
        session.commit()


# ==================== Email Campaigns ====================
# See CLAUDE.md's Email Campaigns section. No queue/worker service exists on
# Railway (single web dyno) — email_worker_loop() below is a lightweight
# in-process asyncio loop started from on_startup() instead of standing up
# Celery/Redis, matching this codebase's existing minimalism.

EMAIL_WORKER_INTERVAL_SECONDS = 15
EMAIL_WORKER_BATCH_SIZE = 100
AUDIENCE_ACTIVE_WINDOW_DAYS = 90


def cta_button_html(label: str, url: str) -> str:
    return (
        f'<a href="{url}" style="display:inline-block;margin:18px 0;padding:12px 22px;'
        f'background:#14CFCF;color:#06181A;font-weight:700;font-size:14px;border-radius:8px;'
        f'text-decoration:none;font-family:Arial,Helvetica,sans-serif;">{html.escape(label)}</a>'
    )


def _welcome_series_defaults():
    # Real, deliberately short copy (not lorem ipsum) — this is what a fresh
    # signup actually receives. [first_name] is substituted at send time by
    # render_email_html(); the CTA links point at real routes.
    step1 = (
        "<p>Hi [first_name],</p>"
        "<p>Welcome to Rink Collective — the fastest way to find ice time, gear, and your people at rinks near you.</p>"
        "<p>Start by finding your home rink and seeing what's happening on the ice this week.</p>"
        + cta_button_html("Explore rinks near you", f"{SITE_BASE_URL}/rinks")
        + "<p>See you on the ice,<br>The Rink Collective team</p>"
    )
    step2 = (
        "<p>Hi [first_name],</p>"
        "<p>Have you set your home rink yet? It powers your check-ins, schedule, and the rinks we recommend to you.</p>"
        + cta_button_html("Find your home rink", f"{SITE_BASE_URL}/rinks")
        + "<p>See you on the ice,<br>The Rink Collective team</p>"
    )
    step3 = (
        "<p>Hi [first_name],</p>"
        "<p>Rinks with real photos help other skaters know what to expect. Got a shot from your last session? Add it to your rink's gallery.</p>"
        + cta_button_html("Add your first photo", f"{SITE_BASE_URL}/rinks")
        + "<p>See you on the ice,<br>The Rink Collective team</p>"
    )
    return [
        (1, 0, "Welcome to Rink Collective", step1),
        (2, 2, "Find your home rink", step2),
        (3, 5, "Add your first photo", step3),
    ]


def seed_welcome_series():
    # One-time seed — this campaign is a pure DB row (like RinkAdmin/
    # IceSession) with no JSON file behind it, so it's never resynced; once
    # created, subsequent startups leave it (and any admin edits) alone.
    with Session(engine) as session:
        if session.exec(select(EmailCampaign).where(EmailCampaign.kind == "automation")).first():
            return
        campaign = EmailCampaign(kind="automation", name="New Signup Welcome Series", status="active", active=True)
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        for order, wait_days, subject, body in _welcome_series_defaults():
            session.add(EmailAutomationStep(
                campaignId=campaign.id, order=order, waitDays=wait_days, subject=subject, bodyHtml=body,
            ))
        session.commit()


def enter_welcome_series(session: Session, user: User):
    # Called from signup and the Google callback's new-account branch.
    campaign = session.exec(select(EmailCampaign).where(EmailCampaign.kind == "automation")).first()
    if campaign and campaign.active:
        session.add(EmailAutomationEntry(campaignId=campaign.id, userId=user.id))


def get_automation_campaign(session: Session) -> EmailCampaign:
    # There's only ever one automation (the Welcome Series) — no generic
    # automation builder yet, so admin endpoints look it up by kind rather
    # than by id.
    campaign = session.exec(select(EmailCampaign).where(EmailCampaign.kind == "automation")).first()
    if campaign is None:
        raise HTTPException(404, "Welcome Series not found")
    return campaign


def audience_filter_conditions(activity: str):
    # Shared by audience_count()/audience_users()/the reach preview endpoint.
    # "active" = last seen (lastLoginAt, falling back to createdAt for a user
    # who never logged back in) within the last 90 days; "inactive" = older
    # than that; "all" = everyone. Unsubscribed users are always excluded.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=AUDIENCE_ACTIVE_WINDOW_DAYS)).isoformat()
    last_seen = func.coalesce(User.lastLoginAt, User.createdAt)
    conds = [User.emailUnsubscribedAt.is_(None)]
    if activity == "active":
        conds.append(last_seen >= cutoff)
    elif activity == "inactive":
        conds.append(last_seen < cutoff)
    return conds


def audience_count(session: Session, activity: str) -> int:
    return session.exec(select(func.count(User.id)).where(*audience_filter_conditions(activity))).one()


def audience_users(session: Session, activity: str) -> list:
    return session.exec(select(User).where(*audience_filter_conditions(activity))).all()


def queue_recipients_for_broadcast(session: Session, campaign: EmailCampaign):
    for user in audience_users(session, campaign.audienceActivity or "all"):
        session.add(EmailRecipient(campaignId=campaign.id, userId=user.id, token=secrets.token_urlsafe(20)))
    session.commit()


def campaign_send_stats(session: Session, campaign_id: int, step_id: Optional[int] = None) -> dict:
    conds = [EmailRecipient.campaignId == campaign_id, EmailRecipient.isTest.is_(False)]
    if step_id is not None:
        conds.append(EmailRecipient.stepId == step_id)
    sent = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.status == "sent")).one()
    opened = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.openedAt.is_not(None))).one()
    clicked = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.clickedAt.is_not(None))).one()
    return {
        "sent": sent,
        "opened": opened,
        "clicked": clicked,
        "openRate": round(100 * opened / sent, 1) if sent else None,
        "clickRate": round(100 * clicked / sent, 1) if sent else None,
    }


def serialize_step(step: EmailAutomationStep, session: Session) -> dict:
    stats = campaign_send_stats(session, step.campaignId, step.id)
    return {
        "id": step.id, "order": step.order, "waitDays": step.waitDays,
        "subject": step.subject, "bodyHtml": step.bodyHtml,
        "sent": stats["sent"], "openRate": stats["openRate"], "clickRate": stats["clickRate"],
    }


def serialize_campaign(campaign: EmailCampaign, session: Session) -> dict:
    # Frontend composes display labels (e.g. "Sent Jul 12") client-side from
    # these raw fields, same convention as the rest of admin.html (relTime()
    # etc.) rather than building formatted strings server-side.
    base = {
        "id": campaign.id, "kind": campaign.kind, "name": campaign.name,
        "createdAt": campaign.createdAt, "updatedAt": campaign.updatedAt,
        "scheduledAt": campaign.scheduledAt, "sentAt": campaign.sentAt,
    }
    if campaign.kind == "automation":
        steps = session.exec(
            select(EmailAutomationStep).where(EmailAutomationStep.campaignId == campaign.id).order_by(EmailAutomationStep.order)
        ).all()
        stats = campaign_send_stats(session, campaign.id)
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        entered_30d = session.exec(
            select(func.count(EmailAutomationEntry.id)).where(
                EmailAutomationEntry.campaignId == campaign.id, EmailAutomationEntry.enteredAt >= thirty_days_ago
            )
        ).one()
        base.update({
            "status": "active" if campaign.active else "paused",
            "active": campaign.active,
            "steps": [serialize_step(s, session) for s in steps],
            "openRate": stats["openRate"], "clickRate": stats["clickRate"],
            "entered30d": entered_30d,
        })
        return base
    activity = campaign.audienceActivity or "all"
    stats = campaign_send_stats(session, campaign.id) if campaign.status == "sent" else {"openRate": None, "clickRate": None}
    base.update({
        "status": campaign.status,
        "subject": campaign.subject, "bodyHtml": campaign.bodyHtml, "ctaUrl": campaign.ctaUrl,
        "audienceActivity": activity,
        "reach": audience_count(session, activity),
        "openRate": stats["openRate"], "clickRate": stats["clickRate"],
    })
    return base


def campaign_report(campaign: EmailCampaign, session: Session) -> dict:
    conds = [EmailRecipient.campaignId == campaign.id, EmailRecipient.isTest.is_(False)]
    delivered = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.status == "sent")).one()
    opened = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.openedAt.is_not(None))).one()
    clicked = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.clickedAt.is_not(None))).one()
    unsubscribed = session.exec(select(func.count(EmailRecipient.id)).where(*conds, EmailRecipient.unsubscribedAt.is_not(None))).one()
    converted = 0
    if campaign.ctaUrl:
        converted = session.exec(
            select(func.count(func.distinct(EmailClick.recipientId)))
            .select_from(EmailClick).join(EmailRecipient, EmailClick.recipientId == EmailRecipient.id)
            .where(*conds, EmailClick.url == campaign.ctaUrl)
        ).one()
    top_links = session.exec(
        select(EmailClick.url, func.count(func.distinct(EmailClick.recipientId)))
        .select_from(EmailClick).join(EmailRecipient, EmailClick.recipientId == EmailRecipient.id)
        .where(*conds)
        .group_by(EmailClick.url)
        .order_by(func.count(func.distinct(EmailClick.recipientId)).desc())
        .limit(3)
    ).all()
    pct = lambda n: round(100 * n / delivered, 1) if delivered else 0
    return {
        "delivered": delivered, "opened": opened, "clicked": clicked,
        "unsubscribed": unsubscribed, "converted": converted,
        "openRate": pct(opened), "clickRate": pct(clicked),
        "unsubscribeRate": round(100 * unsubscribed / delivered, 2) if delivered else 0,
        "convertedRate": pct(converted),
        "topLinks": [{"url": row[0], "clicks": row[1]} for row in top_links],
    }


_LINK_HREF_RE = re.compile(r'href="([^"]+)"')


def wrap_links_for_tracking(body_html: str, token: str) -> str:
    def repl(m):
        url = m.group(1)
        if url.startswith("mailto:") or url.startswith("#") or "/api/email/" in url:
            return m.group(0)
        wrapped = f"{SITE_BASE_URL}/api/email/c/{token}?u={quote(url, safe='')}"
        return f'href="{wrapped}"'
    return _LINK_HREF_RE.sub(repl, body_html)


def render_email_html(subject: str, body_html: str, recipient: User, token: str) -> str:
    first_name = recipient.firstName or (recipient.displayName or "").split(" ")[0] or "there"
    body = body_html.replace("[first_name]", html.escape(first_name))
    body = wrap_links_for_tracking(body, token)
    unsubscribe_url = f"{SITE_BASE_URL}/api/email/u/{token}"
    pixel = f'<img src="{SITE_BASE_URL}/api/email/o/{token}" width="1" height="1" alt="" style="display:block;border:0;">'
    return f"""<!doctype html>
<html>
<body style="margin:0;padding:0;background:#F1F3F8;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:0 auto;background:#FFFFFF;">
    <div style="background:#0A0E1A;padding:22px 26px;">
      <span style="font-family:Arial,Helvetica,sans-serif;font-weight:700;font-size:17px;color:#FFFFFF;">Rink<span style="color:#FFC83D;">Collective</span></span>
    </div>
    <div style="padding:30px 26px;color:#3A4155;font-size:14px;line-height:1.65;">
      <h1 style="font-family:Arial,Helvetica,sans-serif;font-size:20px;font-weight:700;color:#0A0E1A;margin:0 0 14px;">{html.escape(subject)}</h1>
      {body}
    </div>
    <div style="background:#F1F3F8;padding:20px 26px;color:#888;font-size:11px;line-height:1.6;">
      Rink Collective &middot; Boston, MA<br>
      You're receiving this because you have a Rink Collective account.
      <a href="{unsubscribe_url}" style="color:#656C84;text-decoration:underline;">Unsubscribe</a> &middot;
      <a href="{unsubscribe_url}" style="color:#656C84;text-decoration:underline;">Manage preferences</a>
    </div>
  </div>
  {pixel}
</body>
</html>"""


async def send_email_batch(messages: list) -> list:
    # messages: [{"to", "subject", "html"}, ...], max EMAIL_WORKER_BATCH_SIZE.
    # Returns a parallel list of Resend message ids, or None per message on
    # failure (missing key, HTTP error, or a >=400 response) — the worker
    # marks those rows "failed" rather than retrying indefinitely.
    if not RESEND_API_KEY:
        return [None] * len(messages)
    payload = [
        {"from": EMAIL_FROM_ADDRESS, "to": [m["to"]], "subject": m["subject"], "html": m["html"]}
        for m in messages
    ]
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails/batch",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=payload,
                timeout=30,
            )
        if resp.status_code >= 400:
            return [None] * len(messages)
        data = resp.json().get("data", [])
        return [item.get("id") for item in data]
    except httpx.HTTPError:
        return [None] * len(messages)


async def email_worker_tick():
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # 1. Fire due scheduled broadcasts.
        due_broadcasts = session.exec(
            select(EmailCampaign).where(EmailCampaign.status == "scheduled", EmailCampaign.scheduledAt <= now_iso)
        ).all()
        for campaign in due_broadcasts:
            queue_recipients_for_broadcast(session, campaign)
            campaign.status = "sent"
            campaign.sentAt = now_iso
            session.add(campaign)
        if due_broadcasts:
            session.commit()

        # 2. Advance automation entries whose next step is due.
        entries = session.exec(select(EmailAutomationEntry).where(EmailAutomationEntry.completedAt.is_(None))).all()
        for entry in entries:
            campaign = session.get(EmailCampaign, entry.campaignId)
            if not campaign or not campaign.active:
                continue
            steps = session.exec(
                select(EmailAutomationStep).where(EmailAutomationStep.campaignId == entry.campaignId).order_by(EmailAutomationStep.order)
            ).all()
            step = next((s for s in steps if s.order == entry.nextStepOrder), None)
            if step is None:
                entry.completedAt = now_iso
                session.add(entry)
                continue
            due_at = datetime.fromisoformat(entry.enteredAt) + timedelta(days=step.waitDays)
            if now < due_at:
                continue
            already_sent = session.exec(
                select(EmailRecipient).where(
                    EmailRecipient.campaignId == entry.campaignId,
                    EmailRecipient.stepId == step.id,
                    EmailRecipient.userId == entry.userId,
                )
            ).first()
            if not already_sent:
                session.add(EmailRecipient(
                    campaignId=entry.campaignId, stepId=step.id, userId=entry.userId,
                    token=secrets.token_urlsafe(20),
                ))
            entry.nextStepOrder += 1
            if entry.nextStepOrder > len(steps):
                entry.completedAt = now_iso
            session.add(entry)
        session.commit()

        # 3. Drain queued recipients (broadcast or automation-step sends).
        queued = session.exec(
            select(EmailRecipient).where(EmailRecipient.status == "queued").limit(EMAIL_WORKER_BATCH_SIZE)
        ).all()
        if not queued:
            return

        prepared = []
        for r in queued:
            user = session.get(User, r.userId)
            if not user or user.emailUnsubscribedAt:
                r.status = "failed"
                session.add(r)
                continue
            if r.stepId:
                step = session.get(EmailAutomationStep, r.stepId)
                subject, body = step.subject, step.bodyHtml
            else:
                campaign = session.get(EmailCampaign, r.campaignId)
                subject, body = campaign.subject, campaign.bodyHtml
            prepared.append((r, user.email, subject, render_email_html(subject, body, user, r.token)))
        session.commit()

        if not prepared:
            return
        message_ids = await send_email_batch([
            {"to": to, "subject": subject, "html": rendered} for (_, to, subject, rendered) in prepared
        ])
        sent_at = datetime.now(timezone.utc).isoformat()
        for (r, _, _, _), message_id in zip(prepared, message_ids):
            r.status = "sent" if message_id else "failed"
            r.sentAt = sent_at
            r.resendMessageId = message_id
            session.add(r)
        session.commit()


async def email_worker_loop():
    while True:
        try:
            await email_worker_tick()
        except Exception as exc:
            # No logging framework in this codebase yet — print() is the
            # existing ad hoc convention for background-process visibility.
            print(f"[email_worker] tick failed: {exc}")
        await asyncio.sleep(EMAIL_WORKER_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup():
    SQLModel.metadata.create_all(engine)
    ensure_new_columns()
    sync_rinks_from_file()
    sync_equipment_from_file()
    sync_guides_from_file()
    seed_welcome_series()
    asyncio.create_task(email_worker_loop())


@app.get("/")
def root():
    return FileResponse("static/home.html")


@app.get("/rinks")
def rink_finder_page():
    return FileResponse("static/index.html")


@app.get("/equipment")
def equipment_page():
    return FileResponse("static/equipment.html")


@app.get("/guides")
def guides_page():
    return FileResponse("static/guides.html")


@app.get("/rink-admin")
def rink_owner_console_page():
    return FileResponse("static/rink-admin.html")


@app.get("/api/rinks")
def get_rinks(request: Request):
    with Session(engine) as session:
        rinks = session.exec(select(Rink)).all()
        user_id = request.session.get("user_id")
        user = session.get(User, user_id) if user_id else None
        is_admin = bool(user and user.email in ADMIN_EMAILS)
        if not is_admin:
            rinks = [r for r in rinks if not r.adminOnly]
        return [rink.model_dump() for rink in rinks]


def serialize_equipment(product: Equipment, session: Session) -> dict:
    data = product.model_dump()
    offers = session.exec(
        select(EquipmentOffer).where(EquipmentOffer.equipmentId == product.id)
    ).all()
    if not offers:
        # No live offers matched yet for this product — serve the curated
        # mock pricing/retailers from equipment.json untouched.
        return data

    data["retailers"] = [
        {"name": o.retailerName, "price": o.price, "url": o.url, "inStock": o.inStock}
        for o in sorted(offers, key=lambda o: o.price)
    ]

    best_offer = min(offers, key=lambda o: o.price)
    snapshots = session.exec(
        select(EquipmentPriceSnapshot)
        .where(EquipmentPriceSnapshot.equipmentOfferId == best_offer.id)
        .order_by(EquipmentPriceSnapshot.checkedAt)
    ).all()
    price_history = [s.price for s in snapshots] or [best_offer.price]
    was_price = price_history[0] if len(price_history) > 1 else None
    price_is_good = was_price is not None and best_offer.price < was_price
    data.update({
        "priceHistory": price_history,
        "wasPrice": was_price,
        "priceIsGood": price_is_good,
        "deal": f"−{round((1 - best_offer.price / was_price) * 100)}%" if price_is_good else None,
        "note": "Lowest in 90 days" if price_is_good else "Stable price",
    })
    return data


@app.get("/api/equipment")
def get_equipment():
    with Session(engine) as session:
        products = session.exec(select(Equipment)).all()
        return [serialize_equipment(product, session) for product in products]


@app.get("/api/guides")
def get_guides():
    with Session(engine) as session:
        guides = session.exec(select(Guide)).all()
        return [guide.model_dump() for guide in guides]


@app.get("/api/guides/progress")
def get_guide_progress(request: Request):
    user_id = request.session.get("user_id")
    if user_id is None:
        return {}
    with Session(engine) as session:
        rows = session.exec(select(GuideProgress).where(GuideProgress.userId == user_id)).all()
        return {row.guideId: row.completed for row in rows}


@app.post("/api/guides/progress/{guide_id}")
async def set_guide_progress(guide_id: str, request: Request):
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in to track your progress")
    body = await request.json()
    completed = bool(body.get("completed", True))
    with Session(engine) as session:
        row = session.exec(
            select(GuideProgress).where(
                GuideProgress.userId == user_id, GuideProgress.guideId == guide_id
            )
        ).first()
        if row is None:
            row = GuideProgress(userId=user_id, guideId=guide_id, completed=completed)
        else:
            row.completed = completed
            row.updatedAt = datetime.now(timezone.utc).isoformat()
        session.add(row)
        session.commit()
    return {"status": "ok", "completed": completed}


@app.get("/api/photos/{rink_id}/{photo_idx}")
async def rink_photo(rink_id: int, photo_idx: int):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(404, "Photo proxy not configured")
    with Session(engine) as session:
        rink = session.get(Rink, rink_id)
    if rink is None or photo_idx < 0 or photo_idx >= len(rink.photos):
        raise HTTPException(404, "Photo not found")
    ref = rink.photos[photo_idx]["ref"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://places.googleapis.com/v1/{ref}/media",
            params={"maxWidthPx": 640, "skipHttpRedirect": "true"},
            headers={"X-Goog-Api-Key": GOOGLE_PLACES_API_KEY},
        )
    if resp.status_code != 200:
        raise HTTPException(404, "Photo unavailable")
    photo_uri = resp.json()["photoUri"]
    return RedirectResponse(photo_uri, headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/rinks/submit")
async def submit_rink(request: Request):
    rink = await request.json()
    with Session(engine) as session:
        session.add(PendingRink(data=rink))
        session.commit()
    return {"status": "received"}


MAX_PHOTO_BYTES = 8 * 1024 * 1024

PHOTO_SIGNATURES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89\x50\x4e\x47": "image/png",
}


@app.post("/api/rinks/{rink_id}/photos")
async def upload_rink_photo(
    rink_id: int,
    request: Request,
    file: UploadFile = File(...),
    caption: str = Form(""),
):
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in to add a photo")

    with Session(engine) as session:
        if session.get(Rink, rink_id) is None:
            raise HTTPException(404, "Rink not found")

        data = await file.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "Photo must be under 8MB")

        content_type = next(
            (ct for sig, ct in PHOTO_SIGNATURES.items() if data.startswith(sig)), None
        )
        if content_type is None:
            raise HTTPException(400, "Please choose a JPG or PNG photo")

        photo = RinkPhoto(
            rinkId=rink_id,
            userId=user_id,
            data=data,
            contentType=content_type,
            caption=caption.strip() or None,
        )
        session.add(photo)
        session.commit()

    return {"status": "pending_review"}


@app.post("/api/auth/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    # Same size/type validation as upload_rink_photo above, reusing its
    # MAX_PHOTO_BYTES/PHOTO_SIGNATURES constants — but no moderation queue:
    # an avatar only ever displays next to its own uploader's name, so the
    # same trust model as displayName itself applies.
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in required")

    data = await file.read()
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo must be under 8MB")

    content_type = next(
        (ct for sig, ct in PHOTO_SIGNATURES.items() if data.startswith(sig)), None
    )
    if content_type is None:
        raise HTTPException(400, "Please choose a JPG or PNG photo")

    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(401, "Sign in required")
        user.avatarData = data
        user.avatarContentType = content_type
        session.add(user)
        session.commit()
        session.refresh(user)
        return user_public(user)


@app.get("/api/users/{user_id}/avatar")
def user_avatar_image(user_id: int):
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None or not user.avatarData:
            raise HTTPException(404, "No avatar")
        return Response(
            content=user.avatarData,
            media_type=user.avatarContentType,
            headers={"Cache-Control": "public, max-age=3600"},
        )


@app.get("/admin")
def admin_console_page():
    return FileResponse("static/admin.html")


@app.get("/admin/photos")
def admin_photos_redirect():
    # Old bare-bones photo-review page's route, kept for bookmark
    # compatibility — now redirects into the full Admin Console.
    return RedirectResponse("/admin?view=photos")


def serialize_admin_photo(photo: RinkPhoto, session: Session) -> dict:
    rink = session.get(Rink, photo.rinkId)
    user = session.get(User, photo.userId)
    return {
        "id": photo.id,
        "rinkId": photo.rinkId,
        "rinkName": rink.name if rink else "(deleted rink)",
        "rinkCity": rink.city if rink else "",
        "rinkState": rink.state if rink else "",
        "rinkType": rink.type if rink else "STANDARD",
        "caption": photo.caption,
        "submittedAt": photo.submittedAt,
        "submitterName": user.displayName if user else "(deleted user)",
        "submitterEmail": user.email if user else "",
    }


@app.get("/api/admin/photos")
def list_admin_photos(request: Request, status: str = "pending"):
    with Session(engine) as session:
        require_admin(request, session)
        photos = session.exec(select(RinkPhoto).where(RinkPhoto.status == status)).all()
        pending_count = session.exec(select(func.count(RinkPhoto.id)).where(RinkPhoto.status == "pending")).one()
        approved_count = session.exec(select(func.count(RinkPhoto.id)).where(RinkPhoto.status == "approved")).one()
        return {
            "items": [serialize_admin_photo(p, session) for p in photos],
            "counts": {"pending": pending_count, "approved": approved_count},
        }


@app.get("/api/admin/photos/{photo_id}/image")
def admin_photo_image(photo_id: int, request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        photo = session.get(RinkPhoto, photo_id)
        if photo is None:
            raise HTTPException(404, "Photo not found")
        return Response(content=photo.data, media_type=photo.contentType)


@app.post("/api/admin/photos/{photo_id}/approve")
def approve_photo(photo_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        photo = session.get(RinkPhoto, photo_id)
        if photo is None:
            raise HTTPException(404, "Photo not found")
        rink = session.get(Rink, photo.rinkId)
        photo.status = "approved"
        session.add(photo)
        log_activity(session, "photo", f"{admin.displayName} approved a photo of {rink.name if rink else 'a rink'}", admin)
        session.commit()
    return {"status": "approved"}


@app.post("/api/admin/photos/{photo_id}/reject")
async def reject_photo(photo_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        body = await request.json()
        reason = (body.get("reason") or "").strip()
        photo = session.get(RinkPhoto, photo_id)
        if photo is None:
            raise HTTPException(404, "Photo not found")
        rink = session.get(Rink, photo.rinkId)
        text_ = f"{admin.displayName} rejected a photo of {rink.name if rink else 'a rink'}"
        if reason:
            text_ += f" — {reason}"
        log_activity(session, "reject", text_, admin)
        session.delete(photo)
        session.commit()
    return {"status": "rejected"}


def hours_summary(hours: dict) -> str:
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    values = [hours.get(d, "") for d in days]
    if not any(values):
        return "Hours not set"
    if all(v == values[0] for v in values):
        return f"Daily {values[0]}"
    weekday, weekend = values[:5], values[5:]
    if all(v == weekday[0] for v in weekday) and all(v == weekend[0] for v in weekend):
        return f"Weekdays {weekday[0]} · Weekends {weekend[0]}"
    return "Hours vary by day"


@app.get("/api/admin/users")
def list_admin_users(request: Request, q: str = "", page: int = 1):
    page_size = 50
    with Session(engine) as session:
        require_admin(request, session)
        query = select(User)
        if q:
            like = f"%{q.lower()}%"
            query = query.where(
                (func.lower(User.displayName).like(like)) | (func.lower(User.email).like(like))
            )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        users = session.exec(
            query.order_by(User.createdAt.desc()).offset((page - 1) * page_size).limit(page_size)
        ).all()
        items = []
        for u in users:
            contributed = session.exec(
                select(RinkPhoto).where(RinkPhoto.userId == u.id, RinkPhoto.status == "approved")
            ).first() is not None
            role = "superadmin" if u.email in ADMIN_EMAILS else ("contributor" if contributed else "member")
            items.append({
                "id": u.id, "name": u.displayName, "email": u.email,
                "joined": u.createdAt, "logins": u.loginCount, "role": role,
            })
        return {"items": items, "total": total, "page": page, "pageSize": page_size}


@app.get("/api/admin/rinks")
def list_admin_rinks(request: Request, q: str = "", page: int = 1):
    page_size = 50
    with Session(engine) as session:
        require_admin(request, session)
        query = select(Rink)
        if q:
            like = f"%{q.lower()}%"
            query = query.where(
                (func.lower(Rink.name).like(like)) | (func.lower(Rink.city).like(like))
            )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        rinks = session.exec(
            query.order_by(Rink.name).offset((page - 1) * page_size).limit(page_size)
        ).all()
        items = [{
            "id": r.id, "name": r.name, "city": f"{r.city}, {r.state}",
            "type": r.type, "phone": r.phone or "",
            "hoursSummary": hours_summary(r.hours),
        } for r in rinks]
        return {"items": items, "total": total, "page": page, "pageSize": page_size}


@app.get("/api/admin/rinks/{rink_id}")
def get_admin_rink(rink_id: int, request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        rink = session.get(Rink, rink_id)
        if rink is None:
            raise HTTPException(404, "Rink not found")
        return rink.model_dump()


@app.patch("/api/admin/rinks/{rink_id}")
async def update_admin_rink(rink_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        body = await request.json()
        rink = session.get(Rink, rink_id)
        if rink is None:
            raise HTTPException(404, "Rink not found")
        rink.type = body.get("type", rink.type)
        rink.phone = body.get("phone", rink.phone)
        rink.address = body.get("address", rink.address)
        rink.hours = body.get("hours", rink.hours)
        rink.amenities = body.get("amenities", rink.amenities)
        rink.adminEditedAt = datetime.now(timezone.utc).isoformat()
        session.add(rink)
        log_activity(session, "rink", f"{admin.displayName} updated {rink.name}", admin)
        session.commit()
        session.refresh(rink)
        return rink.model_dump()


@app.get("/api/admin/rinks/{rink_id}/admins")
def list_rink_admins(rink_id: int, request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        grants = session.exec(select(RinkAdmin).where(RinkAdmin.rinkId == rink_id)).all()
        items = []
        for g in grants:
            user = session.get(User, g.userId)
            items.append({
                "id": g.id, "userId": g.userId,
                "name": user.displayName if user else "(deleted user)",
                "email": user.email if user else "",
                "grantedAt": g.createdAt,
            })
        return items


@app.post("/api/admin/rinks/{rink_id}/admins")
async def add_rink_admin(rink_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        rink = session.get(Rink, rink_id)
        if rink is None:
            raise HTTPException(404, "Rink not found")
        body = await request.json()
        email = body.get("email", "").strip().lower()
        target = session.exec(select(User).where(User.email == email)).first()
        if target is None:
            raise HTTPException(404, "No RinkCollective account with that email — they need to sign up first")
        existing = session.exec(
            select(RinkAdmin).where(RinkAdmin.userId == target.id, RinkAdmin.rinkId == rink_id)
        ).first()
        if existing is not None:
            raise HTTPException(409, "That user already manages this rink")
        grant = RinkAdmin(userId=target.id, rinkId=rink_id)
        session.add(grant)
        log_activity(session, "rink", f"{admin.displayName} granted {target.displayName} Rink Admin access to {rink.name}", admin)
        session.commit()
        session.refresh(grant)
        return {"id": grant.id, "userId": target.id, "name": target.displayName, "email": target.email, "grantedAt": grant.createdAt}


@app.delete("/api/admin/rinks/{rink_id}/admins/{admin_id}")
def remove_rink_admin(rink_id: int, admin_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        grant = session.get(RinkAdmin, admin_id)
        if grant is None or grant.rinkId != rink_id:
            raise HTTPException(404, "Rink admin grant not found")
        rink = session.get(Rink, rink_id)
        target = session.get(User, grant.userId)
        session.delete(grant)
        log_activity(session, "rink", f"{admin.displayName} removed {target.displayName if target else 'a user'}'s Rink Admin access to {rink.name if rink else 'a rink'}", admin)
        session.commit()
    return {"status": "removed"}


SESSION_TYPES = {"Stick & Puck", "Drop-In", "Public Skate", "Learn to Skate", "Hockey League"}


@app.get("/api/rink-admin/rinks")
def list_my_rink_admin_rinks(request: Request):
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in required")
    with Session(engine) as session:
        grants = session.exec(select(RinkAdmin).where(RinkAdmin.userId == user_id)).all()
        rinks = [session.get(Rink, g.rinkId) for g in grants]
        return [{"id": r.id, "name": r.name} for r in rinks if r]


def serialize_ice_sheet(sheet: IceSheet, session: Session) -> dict:
    count = session.exec(
        select(func.count(IceSession.id)).where(IceSession.sheetId == sheet.id)
    ).one()
    return {"id": sheet.id, "name": sheet.name, "sortOrder": sheet.sortOrder, "sessionCount": count}


@app.get("/api/rink-admin/{rink_id}/sheets")
def list_ice_sheets(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        sheets = session.exec(
            select(IceSheet).where(IceSheet.rinkId == rink_id).order_by(IceSheet.sortOrder)
        ).all()
        return [serialize_ice_sheet(sh, session) for sh in sheets]


@app.put("/api/rink-admin/{rink_id}/sheets")
async def save_ice_sheets(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        body = await request.json()
        items = body.get("sheets", [])
        if not items:
            raise HTTPException(400, "At least one ice sheet is required")

        existing = {
            sh.id: sh for sh in session.exec(select(IceSheet).where(IceSheet.rinkId == rink_id)).all()
        }
        keep_ids = {item["id"] for item in items if item.get("id")}
        for stale_id, sheet in existing.items():
            if stale_id not in keep_ids:
                for s in session.exec(select(IceSession).where(IceSession.sheetId == stale_id)).all():
                    session.delete(s)
                session.delete(sheet)

        result = []
        for idx, item in enumerate(items):
            sid = item.get("id")
            if sid and sid in existing and sid in keep_ids:
                sheet = existing[sid]
                sheet.name = item["name"]
                sheet.sortOrder = idx
            else:
                sheet = IceSheet(rinkId=rink_id, name=item["name"], sortOrder=idx)
            session.add(sheet)
            result.append(sheet)
        session.commit()
        for sheet in result:
            session.refresh(sheet)
        return [serialize_ice_sheet(sh, session) for sh in result]


@app.get("/api/rink-admin/{rink_id}/sessions")
def list_ice_sessions(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        sessions = session.exec(select(IceSession).where(IceSession.rinkId == rink_id)).all()
        return [s.model_dump() for s in sessions]


@app.post("/api/rink-admin/{rink_id}/sessions")
async def create_ice_session(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        body = await request.json()
        sheet = session.get(IceSheet, body.get("sheetId"))
        if sheet is None or sheet.rinkId != rink_id:
            raise HTTPException(404, "Ice sheet not found")
        session_type = body.get("type")
        if session_type not in SESSION_TYPES:
            raise HTTPException(400, "Invalid session type")

        sess = IceSession(
            rinkId=rink_id,
            sheetId=sheet.id,
            type=session_type,
            dayOfWeek=body.get("dayOfWeek"),
            date=body.get("date"),
            start=body.get("start", ""),
            end=body.get("end", ""),
            price=body.get("price", 0),
            cap=body.get("cap", 0),
            ages=body.get("ages", ""),
            repeatWeekly=body.get("repeatWeekly", True),
        )
        session.add(sess)
        session.commit()
        session.refresh(sess)

        if session_type == "Learn to Skate":
            program = Program(
                rinkId=rink_id,
                name=f"Learn to Skate — {sheet.name}",
                type=session_type,
                price=sess.price,
                cap=sess.cap,
                ageRange=sess.ages,
                status="draft",
                linkedSessionId=sess.id,
            )
            session.add(program)
            session.commit()
            session.refresh(program)
            sess.linkedProgramId = program.id
            session.add(sess)
            session.commit()
            session.refresh(sess)

        return sess.model_dump()


@app.delete("/api/rink-admin/{rink_id}/sessions/{session_id}")
def delete_ice_session(rink_id: int, session_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        sess = session.get(IceSession, session_id)
        if sess is None or sess.rinkId != rink_id:
            raise HTTPException(404, "Session not found")
        session.delete(sess)
        session.commit()
    return {"status": "removed"}


@app.post("/api/rink-admin/{rink_id}/sessions/copy-last-week")
async def copy_last_week(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        body = await request.json()
        try:
            target_monday = date.fromisoformat(body.get("weekStart", ""))
        except ValueError:
            raise HTTPException(400, "weekStart must be an ISO date (the Monday of the week being copied into)")
        source_monday = target_monday - timedelta(days=7)

        one_time = session.exec(
            select(IceSession).where(IceSession.rinkId == rink_id, IceSession.repeatWeekly.is_(False))
        ).all()
        created = []
        for s in one_time:
            if not s.date:
                continue
            s_date = date.fromisoformat(s.date)
            if source_monday <= s_date <= source_monday + timedelta(days=6):
                new_date = target_monday + timedelta(days=(s_date - source_monday).days)
                new_sess = IceSession(
                    rinkId=rink_id, sheetId=s.sheetId, type=s.type, date=new_date.isoformat(),
                    start=s.start, end=s.end, price=s.price, cap=s.cap, ages=s.ages, repeatWeekly=False,
                )
                session.add(new_sess)
                created.append(new_sess)
        session.commit()
        for c in created:
            session.refresh(c)
        return [c.model_dump() for c in created]


ANNOUNCEMENT_TYPES = {"alert", "update", "event"}
ANNOUNCEMENT_AUDIENCES = {"everyone", "league", "program_families"}


@app.get("/api/rink-admin/{rink_id}/announcements")
def list_rink_admin_announcements(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        rows = session.exec(
            select(Announcement).where(Announcement.rinkId == rink_id)
            .order_by(Announcement.pinned.desc(), Announcement.createdAt.desc())
        ).all()
        return [r.model_dump() for r in rows]


@app.post("/api/rink-admin/{rink_id}/announcements")
async def create_announcement(rink_id: int, request: Request):
    with Session(engine) as session:
        admin = require_rink_admin(request, session, rink_id)
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            raise HTTPException(400, "Announcement text is required")
        ann_type = body.get("type", "update")
        audience = body.get("audience", "everyone")
        if ann_type not in ANNOUNCEMENT_TYPES or audience not in ANNOUNCEMENT_AUDIENCES:
            raise HTTPException(400, "Invalid type or audience")
        ann = Announcement(
            rinkId=rink_id, authorId=admin.id, type=ann_type, text=text, audience=audience,
            pinned=bool(body.get("pinned", False)), pushNotify=bool(body.get("pushNotify", False)),
        )
        session.add(ann)
        session.commit()
        session.refresh(ann)
        return ann.model_dump()


@app.patch("/api/rink-admin/{rink_id}/announcements/{announcement_id}")
async def update_announcement(rink_id: int, announcement_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        ann = session.get(Announcement, announcement_id)
        if ann is None or ann.rinkId != rink_id:
            raise HTTPException(404, "Announcement not found")
        body = await request.json()
        text = body.get("text", ann.text).strip()
        ann_type = body.get("type", ann.type)
        audience = body.get("audience", ann.audience)
        if not text:
            raise HTTPException(400, "Announcement text is required")
        if ann_type not in ANNOUNCEMENT_TYPES or audience not in ANNOUNCEMENT_AUDIENCES:
            raise HTTPException(400, "Invalid type or audience")
        ann.text = text
        ann.type = ann_type
        ann.audience = audience
        ann.pinned = bool(body.get("pinned", ann.pinned))
        ann.pushNotify = bool(body.get("pushNotify", ann.pushNotify))
        session.add(ann)
        session.commit()
        session.refresh(ann)
        return ann.model_dump()


@app.delete("/api/rink-admin/{rink_id}/announcements/{announcement_id}")
def delete_announcement(rink_id: int, announcement_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        ann = session.get(Announcement, announcement_id)
        if ann is None or ann.rinkId != rink_id:
            raise HTTPException(404, "Announcement not found")
        session.delete(ann)
        session.commit()
    return {"status": "removed"}


@app.get("/api/rinks/{rink_id}/announcements")
def public_rink_announcements(rink_id: int):
    with Session(engine) as session:
        rows = session.exec(
            select(Announcement).where(Announcement.rinkId == rink_id)
            .order_by(Announcement.pinned.desc(), Announcement.createdAt.desc())
        ).all()
        for r in rows:
            # Approximate "seen" counter, same rigor as the existing
            # checkins/reviewCount fields — not unique-visitor tracked.
            r.seenCount += 1
            session.add(r)
        session.commit()
        for r in rows:
            session.refresh(r)
        return [r.model_dump() for r in rows]


def effective_program_status(program: Program) -> str:
    # "past" and "waitlist" are never stored — they're derived so an owner
    # never has to manually flip a program when its end date passes or its
    # roster fills up. Only draft/open/closed/cancelled are real DB states.
    if program.status == "cancelled":
        return "cancelled"
    if program.endDate:
        try:
            if date.fromisoformat(program.endDate) < date.today():
                return "past"
        except ValueError:
            pass
    return program.status


def serialize_program(program: Program, session: Session, include_roster: bool = False) -> dict:
    registrants = session.exec(
        select(ProgramRegistrant).where(ProgramRegistrant.programId == program.id)
    ).all()
    enrolled = [r for r in registrants if not r.waitlisted]
    waitlisted = [r for r in registrants if r.waitlisted]
    revenue = sum(program.price for r in enrolled if r.payStatus == "paid")

    data = program.model_dump()
    eff = effective_program_status(program)
    if eff == "open" and program.cap > 0 and len(enrolled) >= program.cap:
        eff = "waitlist"
    data["effectiveStatus"] = eff
    data["enrolledCount"] = len(enrolled)
    data["waitlistCount"] = len(waitlisted)
    data["revenue"] = revenue
    if include_roster:
        data["roster"] = [r.model_dump() for r in registrants]
    return data


def promote_waitlisted_registrants(program: Program, session: Session):
    # Moves the oldest waitlisted registrants into enrolled slots as
    # capacity frees up (a registrant cancels, or cap is raised).
    registrants = session.exec(
        select(ProgramRegistrant).where(ProgramRegistrant.programId == program.id)
        .order_by(ProgramRegistrant.createdAt)
    ).all()
    enrolled_count = sum(1 for r in registrants if not r.waitlisted)
    for r in registrants:
        if enrolled_count >= program.cap:
            break
        if r.waitlisted:
            r.waitlisted = False
            session.add(r)
            enrolled_count += 1


@app.get("/api/rink-admin/{rink_id}/programs")
def list_rink_admin_programs(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        programs = session.exec(
            select(Program).where(Program.rinkId == rink_id).order_by(Program.createdAt.desc())
        ).all()
        return [serialize_program(p, session) for p in programs]


@app.post("/api/rink-admin/{rink_id}/programs")
async def create_program(rink_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            raise HTTPException(400, "Program name is required")
        program_type = body.get("type", "")
        if program_type and program_type not in SESSION_TYPES:
            raise HTTPException(400, "Invalid program type")
        program = Program(
            rinkId=rink_id, name=name, type=program_type,
            startDate=body.get("startDate"), endDate=body.get("endDate"),
            ageRange=body.get("ageRange", ""), scheduleNote=body.get("scheduleNote", ""),
            price=body.get("price", 0), cap=body.get("cap", 0), status="draft",
        )
        session.add(program)
        session.commit()
        session.refresh(program)
        return serialize_program(program, session)


def get_owned_program(session: Session, rink_id: int, program_id: int) -> Program:
    program = session.get(Program, program_id)
    if program is None or program.rinkId != rink_id:
        raise HTTPException(404, "Program not found")
    return program


@app.get("/api/rink-admin/{rink_id}/programs/{program_id}")
def get_rink_admin_program(rink_id: int, program_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        program = get_owned_program(session, rink_id, program_id)
        return serialize_program(program, session, include_roster=True)


@app.patch("/api/rink-admin/{rink_id}/programs/{program_id}")
async def update_program(rink_id: int, program_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        program = get_owned_program(session, rink_id, program_id)
        body = await request.json()

        if "name" in body:
            name = body["name"].strip()
            if not name:
                raise HTTPException(400, "Program name is required")
            program.name = name
        if "type" in body:
            if body["type"] and body["type"] not in SESSION_TYPES:
                raise HTTPException(400, "Invalid program type")
            program.type = body["type"]
        if "startDate" in body:
            program.startDate = body["startDate"]
        if "endDate" in body:
            program.endDate = body["endDate"]
        if "ageRange" in body:
            program.ageRange = body["ageRange"]
        if "scheduleNote" in body:
            program.scheduleNote = body["scheduleNote"]
        if "price" in body:
            program.price = body["price"]

        cap_increased = False
        if "cap" in body:
            new_cap = int(body["cap"])
            enrolled_count = session.exec(
                select(func.count(ProgramRegistrant.id)).where(
                    ProgramRegistrant.programId == program_id, ProgramRegistrant.waitlisted.is_(False)
                )
            ).one()
            if new_cap < enrolled_count:
                raise HTTPException(400, "Capacity can't drop below the current enrolled count")
            cap_increased = new_cap > program.cap
            program.cap = new_cap

        if "status" in body:
            if body["status"] not in {"draft", "open", "closed", "cancelled"}:
                raise HTTPException(400, "Invalid status")
            program.status = body["status"]

        session.add(program)
        session.commit()
        session.refresh(program)

        if cap_increased:
            promote_waitlisted_registrants(program, session)
            session.commit()
            session.refresh(program)

        return serialize_program(program, session, include_roster=True)


@app.post("/api/rink-admin/{rink_id}/programs/{program_id}/roster")
async def add_program_registrant(rink_id: int, program_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        program = get_owned_program(session, rink_id, program_id)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            raise HTTPException(400, "Registrant name is required")
        enrolled_count = session.exec(
            select(func.count(ProgramRegistrant.id)).where(
                ProgramRegistrant.programId == program_id, ProgramRegistrant.waitlisted.is_(False)
            )
        ).one()
        registrant = ProgramRegistrant(
            programId=program_id, name=name, age=body.get("age"),
            payStatus=body.get("payStatus", "due"),
            waitlisted=program.cap > 0 and enrolled_count >= program.cap,
        )
        session.add(registrant)
        session.commit()
        session.refresh(registrant)
        return registrant.model_dump()


def get_owned_registrant(session: Session, rink_id: int, program_id: int, registrant_id: int) -> ProgramRegistrant:
    get_owned_program(session, rink_id, program_id)
    registrant = session.get(ProgramRegistrant, registrant_id)
    if registrant is None or registrant.programId != program_id:
        raise HTTPException(404, "Registrant not found")
    return registrant


@app.post("/api/rink-admin/{rink_id}/programs/{program_id}/roster/{registrant_id}/remind")
def remind_registrant(rink_id: int, program_id: int, registrant_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        get_owned_registrant(session, rink_id, program_id, registrant_id)
    # No real SMS/email infra exists yet — this is a UI-only acknowledgement,
    # same stub treatment as the Payments card and pushNotify on Announcements.
    return {"status": "reminded"}


@app.post("/api/rink-admin/{rink_id}/programs/{program_id}/roster/{registrant_id}/message")
def message_registrant(rink_id: int, program_id: int, registrant_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        get_owned_registrant(session, rink_id, program_id, registrant_id)
    return {"status": "messaged"}


@app.delete("/api/rink-admin/{rink_id}/programs/{program_id}/roster/{registrant_id}")
def remove_registrant(rink_id: int, program_id: int, registrant_id: int, request: Request):
    with Session(engine) as session:
        require_rink_admin(request, session, rink_id)
        program = get_owned_program(session, rink_id, program_id)
        registrant = get_owned_registrant(session, rink_id, program_id, registrant_id)
        session.delete(registrant)
        session.commit()
        promote_waitlisted_registrants(program, session)
        session.commit()
    return {"status": "removed"}


def serialize_program_public(program: Program, session: Session) -> dict:
    data = serialize_program(program, session)
    return {
        "id": data["id"], "name": data["name"], "type": data["type"],
        "startDate": data["startDate"], "endDate": data["endDate"],
        "ageRange": data["ageRange"], "scheduleNote": data["scheduleNote"],
        "price": data["price"], "cap": data["cap"],
        "effectiveStatus": data["effectiveStatus"],
        "enrolledCount": data["enrolledCount"], "waitlistCount": data["waitlistCount"],
    }


@app.get("/api/rinks/{rink_id}/programs")
def public_rink_programs(rink_id: int):
    with Session(engine) as session:
        programs = session.exec(select(Program).where(Program.rinkId == rink_id)).all()
        return [
            serialize_program_public(p, session) for p in programs
            if effective_program_status(p) not in ("draft", "cancelled")
        ]


@app.get("/api/rinks/{rink_id}/schedule")
def public_rink_schedule(rink_id: int):
    with Session(engine) as session:
        sheets = session.exec(
            select(IceSheet).where(IceSheet.rinkId == rink_id).order_by(IceSheet.sortOrder)
        ).all()
        sessions = session.exec(select(IceSession).where(IceSession.rinkId == rink_id)).all()
        return {
            "sheets": [{"id": sh.id, "name": sh.name} for sh in sheets],
            "sessions": [s.model_dump() for s in sessions],
        }


@app.get("/api/admin/activity")
def list_admin_activity(request: Request, limit: int = 50):
    with Session(engine) as session:
        require_admin(request, session)
        rows = session.exec(
            select(AdminActivity).order_by(AdminActivity.createdAt.desc()).limit(limit)
        ).all()
        return [r.model_dump() for r in rows]


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        pending_photos = session.exec(select(RinkPhoto).where(RinkPhoto.status == "pending")).all()
        total_members = session.exec(select(func.count(User.id))).one()
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        members_delta = session.exec(
            select(func.count(User.id)).where(User.createdAt >= thirty_days_ago)
        ).one()
        total_rinks = session.exec(select(func.count(Rink.id))).one()
        today = datetime.now(timezone.utc).date().isoformat()
        actions_today = session.exec(
            select(func.count(AdminActivity.id)).where(
                AdminActivity.actorId == admin.id, AdminActivity.createdAt >= today
            )
        ).one()
        recent_activity = session.exec(
            select(AdminActivity).order_by(AdminActivity.createdAt.desc()).limit(6)
        ).all()
        return {
            "pendingPhotos": len(pending_photos),
            "totalMembers": total_members,
            "membersDelta30d": members_delta,
            "totalRinks": total_rinks,
            "actionsToday": actions_today,
            "pendingPreview": [serialize_admin_photo(p, session) for p in pending_photos[:4]],
            "recentActivity": [a.model_dump() for a in recent_activity],
        }


@app.get("/api/admin/campaigns")
def list_campaigns(request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        automation = session.exec(select(EmailCampaign).where(EmailCampaign.kind == "automation")).first()
        broadcasts = session.exec(
            select(EmailCampaign).where(EmailCampaign.kind == "broadcast").order_by(EmailCampaign.createdAt.desc())
        ).all()
        rows = ([automation] if automation else []) + broadcasts
        return [serialize_campaign(c, session) for c in rows]


@app.post("/api/admin/campaigns")
async def create_campaign(request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        body = await request.json()
        name = (body.get("name") or "").strip() or "Untitled broadcast"
        campaign = EmailCampaign(kind="broadcast", name=name, status="draft", audienceActivity="all")
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        log_activity(session, "campaign", f'{admin.displayName} created a new broadcast draft: "{name}"', admin)
        session.commit()
        return serialize_campaign(campaign, session)


@app.get("/api/admin/campaigns/audience-count")
def campaign_audience_count(request: Request, activity: str = "all"):
    with Session(engine) as session:
        require_admin(request, session)
        if activity not in ("active", "inactive", "all"):
            raise HTTPException(400, "Invalid activity segment")
        return {"reach": audience_count(session, activity)}


@app.get("/api/admin/automations/welcome-series")
def get_welcome_series(request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        return serialize_campaign(get_automation_campaign(session), session)


@app.patch("/api/admin/automations/welcome-series")
async def update_welcome_series(request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        campaign = get_automation_campaign(session)
        body = await request.json()
        if "active" in body:
            campaign.active = bool(body["active"])
            session.add(campaign)
            session.commit()
            log_activity(session, "campaign", f"{admin.displayName} {'resumed' if campaign.active else 'paused'} the Welcome Series", admin)
            session.commit()
        return serialize_campaign(campaign, session)


@app.patch("/api/admin/automations/welcome-series/steps/{step_id}")
async def update_welcome_series_step(step_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        campaign = get_automation_campaign(session)
        step = session.get(EmailAutomationStep, step_id)
        if step is None or step.campaignId != campaign.id:
            raise HTTPException(404, "Step not found")
        body = await request.json()
        for field in ("subject", "bodyHtml"):
            if field in body:
                setattr(step, field, body[field])
        if "waitDays" in body:
            wait_days = int(body["waitDays"])
            if wait_days < 0:
                raise HTTPException(400, "waitDays can't be negative")
            step.waitDays = wait_days
        session.add(step)
        session.commit()
        log_activity(session, "campaign", f"{admin.displayName} edited Welcome Series step {step.order}", admin)
        session.commit()
        return serialize_step(step, session)


@app.post("/api/admin/automations/welcome-series/steps")
async def add_welcome_series_step(request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        campaign = get_automation_campaign(session)
        body = await request.json()
        subject = (body.get("subject") or "").strip()
        if not subject:
            raise HTTPException(400, "subject is required")
        wait_days = int(body.get("waitDays") or 0)
        max_order = session.exec(
            select(func.max(EmailAutomationStep.order)).where(EmailAutomationStep.campaignId == campaign.id)
        ).one()
        step = EmailAutomationStep(
            campaignId=campaign.id, order=(max_order or 0) + 1, waitDays=wait_days,
            subject=subject, bodyHtml=body.get("bodyHtml") or "<p>Hi [first_name],</p>",
        )
        session.add(step)
        session.commit()
        session.refresh(step)
        log_activity(session, "campaign", f"{admin.displayName} added a step to the Welcome Series", admin)
        session.commit()
        return serialize_step(step, session)


@app.post("/api/admin/automations/welcome-series/steps/{step_id}/send-test")
async def send_step_test(step_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        if not RESEND_API_KEY:
            raise HTTPException(503, "Email sending isn't configured (RESEND_API_KEY missing)")
        campaign = get_automation_campaign(session)
        step = session.get(EmailAutomationStep, step_id)
        if step is None or step.campaignId != campaign.id:
            raise HTTPException(404, "Step not found")
        session.add(EmailRecipient(
            campaignId=campaign.id, stepId=step.id, userId=admin.id, token=secrets.token_urlsafe(20), isTest=True,
        ))
        session.commit()
        return {"status": "queued", "to": admin.email}


@app.get("/api/admin/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        campaign = session.get(EmailCampaign, campaign_id)
        if campaign is None:
            raise HTTPException(404, "Campaign not found")
        return serialize_campaign(campaign, session)


@app.patch("/api/admin/campaigns/{campaign_id}")
async def update_campaign(campaign_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        campaign = session.get(EmailCampaign, campaign_id)
        if campaign is None or campaign.kind != "broadcast":
            raise HTTPException(404, "Campaign not found")
        if campaign.status != "draft":
            raise HTTPException(400, "Only draft broadcasts can be edited")
        body = await request.json()
        for field in ("name", "subject", "bodyHtml", "ctaUrl"):
            if field in body:
                setattr(campaign, field, body[field])
        if "audienceActivity" in body:
            if body["audienceActivity"] not in ("active", "inactive", "all"):
                raise HTTPException(400, "Invalid activity segment")
            campaign.audienceActivity = body["audienceActivity"]
        campaign.updatedAt = datetime.now(timezone.utc).isoformat()
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        return serialize_campaign(campaign, session)


@app.post("/api/admin/campaigns/{campaign_id}/send-test")
async def send_campaign_test(campaign_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        if not RESEND_API_KEY:
            raise HTTPException(503, "Email sending isn't configured (RESEND_API_KEY missing)")
        campaign = session.get(EmailCampaign, campaign_id)
        if campaign is None or campaign.kind != "broadcast":
            raise HTTPException(404, "Broadcast not found")
        if not campaign.subject or not campaign.bodyHtml:
            raise HTTPException(400, "Add a subject and body before sending a test")
        session.add(EmailRecipient(
            campaignId=campaign.id, userId=admin.id, token=secrets.token_urlsafe(20), isTest=True,
        ))
        session.commit()
        return {"status": "queued", "to": admin.email}


@app.post("/api/admin/campaigns/{campaign_id}/schedule")
async def schedule_campaign(campaign_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        if not RESEND_API_KEY:
            raise HTTPException(503, "Email sending isn't configured (RESEND_API_KEY missing)")
        campaign = session.get(EmailCampaign, campaign_id)
        if campaign is None or campaign.kind != "broadcast":
            raise HTTPException(404, "Broadcast not found")
        if campaign.status != "draft":
            raise HTTPException(400, "Only draft broadcasts can be scheduled")
        if not campaign.subject or not campaign.bodyHtml:
            raise HTTPException(400, "Add a subject and body before scheduling")
        body = await request.json()
        scheduled_at = body.get("scheduledAt")
        if not scheduled_at:
            raise HTTPException(400, "scheduledAt is required")
        campaign.status = "scheduled"
        campaign.scheduledAt = scheduled_at
        session.add(campaign)
        session.commit()
        log_activity(session, "campaign", f'{admin.displayName} scheduled "{campaign.name}"', admin)
        session.commit()
        return serialize_campaign(campaign, session)


@app.post("/api/admin/campaigns/{campaign_id}/send")
async def send_campaign_now(campaign_id: int, request: Request):
    with Session(engine) as session:
        admin = require_admin(request, session)
        if not RESEND_API_KEY:
            raise HTTPException(503, "Email sending isn't configured (RESEND_API_KEY missing)")
        campaign = session.get(EmailCampaign, campaign_id)
        if campaign is None or campaign.kind != "broadcast":
            raise HTTPException(404, "Broadcast not found")
        if campaign.status not in ("draft", "scheduled"):
            raise HTTPException(400, "This broadcast has already been sent")
        if not campaign.subject or not campaign.bodyHtml:
            raise HTTPException(400, "Add a subject and body before sending")
        queue_recipients_for_broadcast(session, campaign)
        campaign.status = "sent"
        campaign.sentAt = datetime.now(timezone.utc).isoformat()
        session.add(campaign)
        session.commit()
        log_activity(session, "campaign", f'{admin.displayName} sent "{campaign.name}"', admin)
        session.commit()
        return serialize_campaign(campaign, session)


@app.get("/api/admin/campaigns/{campaign_id}/report")
def get_campaign_report(campaign_id: int, request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        campaign = session.get(EmailCampaign, campaign_id)
        if campaign is None or campaign.kind != "broadcast":
            raise HTTPException(404, "Broadcast not found")
        if campaign.status != "sent":
            raise HTTPException(400, "This broadcast hasn't been sent yet")
        return {"campaign": serialize_campaign(campaign, session), "report": campaign_report(campaign, session)}


@app.get("/api/rinks/{rink_id}/photos")
def rink_community_photos(rink_id: int):
    with Session(engine) as session:
        photos = session.exec(
            select(RinkPhoto).where(RinkPhoto.rinkId == rink_id, RinkPhoto.status == "approved")
        ).all()
        return [{"id": p.id, "caption": p.caption} for p in photos]


@app.get("/api/user-photos/{photo_id}")
def user_photo_image(photo_id: int):
    with Session(engine) as session:
        photo = session.get(RinkPhoto, photo_id)
        if photo is None or photo.status != "approved":
            raise HTTPException(404, "Photo not found")
        return Response(
            content=photo.data,
            media_type=photo.contentType,
            headers={"Cache-Control": "public, max-age=3600"},
        )


_TRANSPARENT_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")


@app.get("/api/email/o/{token}")
def track_email_open(token: str):
    # 1x1 pixel embedded in every sent email — public, token-gated, no auth.
    with Session(engine) as session:
        recipient = session.exec(select(EmailRecipient).where(EmailRecipient.token == token)).first()
        if recipient and not recipient.openedAt:
            recipient.openedAt = datetime.now(timezone.utc).isoformat()
            session.add(recipient)
            session.commit()
    return Response(content=_TRANSPARENT_GIF, media_type="image/gif", headers={"Cache-Control": "no-store"})


@app.get("/api/email/c/{token}")
def track_email_click(token: str, u: str = SITE_BASE_URL):
    # Every real content link in a sent email is rewritten to this at render
    # time (wrap_links_for_tracking()); logs the click, then redirects on.
    with Session(engine) as session:
        recipient = session.exec(select(EmailRecipient).where(EmailRecipient.token == token)).first()
        if recipient:
            session.add(EmailClick(recipientId=recipient.id, url=u))
            if not recipient.clickedAt:
                recipient.clickedAt = datetime.now(timezone.utc).isoformat()
                session.add(recipient)
            session.commit()
    return RedirectResponse(u)


@app.get("/api/email/u/{token}")
def unsubscribe_email(token: str):
    # One-click, all-campaigns unsubscribe (CAN-SPAM) — sets both the send
    # record and the user's account-wide flag so future audience queries
    # exclude them regardless of which campaign's link they clicked.
    with Session(engine) as session:
        recipient = session.exec(select(EmailRecipient).where(EmailRecipient.token == token)).first()
        if recipient:
            now_iso = datetime.now(timezone.utc).isoformat()
            recipient.unsubscribedAt = now_iso
            session.add(recipient)
            user = session.get(User, recipient.userId)
            if user and not user.emailUnsubscribedAt:
                user.emailUnsubscribedAt = now_iso
                session.add(user)
            session.commit()
    return Response(
        content="""<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;background:#0A0E1A;color:#F3F5FB;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
<div style="text-align:center;max-width:360px;padding:0 20px;"><h1 style="font-size:20px;margin-bottom:10px;">You're unsubscribed</h1><p style="color:#888FA8;font-size:14px;line-height:1.5;">You won't receive any more emails from Rink Collective.</p></div>
</body></html>""",
        media_type="text/html",
    )


@app.post("/api/auth/signup")
async def signup(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    firstName = body.get("firstName", "").strip()
    lastName = body.get("lastName", "").strip()
    displayName = body.get("displayName", "").strip()
    if not displayName and firstName:
        # Defensive fallback only — the client live-suggests "First L." as
        # the user types and lets them edit it; this just covers a
        # JS-disabled/failed client still submitting the form.
        displayName = suggest_display_name(firstName, lastName)
    if not email or not firstName or not displayName or len(password) < 8:
        raise HTTPException(400, "First name, email, display name, and a password of at least 8 characters are required")
    if not (2 <= len(displayName) <= 30):
        raise HTTPException(400, "Display name must be between 2 and 30 characters")
    if body.get("consent") is not True:
        raise HTTPException(400, "You must agree to the Terms of Service and Privacy Policy to sign up")
    with Session(engine) as session:
        if session.exec(select(User).where(User.email == email)).first():
            raise HTTPException(409, "Email already registered")
        user = User(
            email=email, passwordHash=hash_password(password), displayName=displayName,
            firstName=firstName, lastName=lastName or None, loginCount=1,
            consentAcceptedAt=datetime.now(timezone.utc).isoformat(),
            lastLoginAt=datetime.now(timezone.utc).isoformat(),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        log_activity(session, "user", f"{user.displayName} joined")
        enter_welcome_series(session, user)
        session.commit()
        request.session["user_id"] = user.id
        return user_public(user)


@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        if not user or not verify_password(password, user.passwordHash):
            raise HTTPException(401, "Invalid email or password")
        user.loginCount += 1
        user.lastLoginAt = datetime.now(timezone.utc).isoformat()
        session.add(user)
        session.commit()
        request.session["user_id"] = user.id
        return user_public(user)


@app.get("/api/auth/google/login")
async def google_login(request: Request):
    if not GOOGLE_OAUTH_CLIENT_ID:
        return RedirectResponse("/rinks?auth=login&error=google_unavailable")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/auth/google/callback"
    params = httpx.QueryParams({
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error or not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        return RedirectResponse("/rinks?auth=login&error=google_unavailable")
    expected_state = request.session.pop("oauth_state", None)
    if not state or not expected_state or state != expected_state or not code:
        return RedirectResponse("/rinks?auth=login&error=google_unavailable")

    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/auth/google/callback"
    async with httpx.AsyncClient() as client:
        token_resp = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            return RedirectResponse("/rinks?auth=login&error=google_unavailable")
        access_token = token_resp.json()["access_token"]
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            return RedirectResponse("/rinks?auth=login&error=google_unavailable")
        info = userinfo_resp.json()

    email = info.get("email", "").strip().lower()
    if not email:
        return RedirectResponse("/rinks?auth=login&error=google_unavailable")
    first_name = info.get("given_name", "").strip() or "Skater"
    last_name = info.get("family_name", "").strip()

    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        is_new = user is None
        if user is None:
            user = User(
                email=email,
                # Unusable random password — this account can only sign in
                # via Google, never via the email/password form.
                passwordHash=hash_password(secrets.token_urlsafe(32)),
                displayName=suggest_display_name(first_name, last_name),
                firstName=first_name,
                lastName=last_name or None,
                authProvider="google",
                loginCount=1,
                consentAcceptedAt=datetime.now(timezone.utc).isoformat(),
                lastLoginAt=datetime.now(timezone.utc).isoformat(),
            )
            session.add(user)
        else:
            user.loginCount += 1
            user.lastLoginAt = datetime.now(timezone.utc).isoformat()
            session.add(user)
        session.commit()
        session.refresh(user)
        if is_new:
            log_activity(session, "user", f"{user.displayName} joined")
            enter_welcome_series(session, user)
            session.commit()
        request.session["user_id"] = user.id

    return RedirectResponse("/rinks?onboarding=1" if is_new else "/rinks")


@app.post("/api/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"status": "logged_out"}


@app.get("/api/auth/me")
def me(request: Request):
    user_id = request.session.get("user_id")
    if user_id is None:
        return {"user": None}
    with Session(engine) as session:
        user = session.get(User, user_id)
        return {"user": user_public(user) if user else None}


ONBOARDING_SKILL_LEVELS = {"new", "rec", "comp", "coach"}


@app.patch("/api/auth/onboarding")
async def update_onboarding(request: Request):
    # Session-scoped like GET /api/auth/me — always the caller's own
    # account, no id in the path. Called twice from the onboarding wizard
    # (step 1: homeRinkId/skillLevel, step 2: interests+completed) and once
    # from "Skip for now" (completed alone), so fields are only touched when
    # their key is present in the body — same presence-check convention as
    # PATCH /api/admin/rinks/{rink_id}.
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Sign in required")
    body = await request.json()
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(401, "Sign in required")
        if "homeRinkId" in body:
            user.homeRinkId = body["homeRinkId"]
        if "skillLevel" in body:
            level = body["skillLevel"]
            if level is not None and level not in ONBOARDING_SKILL_LEVELS:
                raise HTTPException(400, "Invalid skill level")
            user.skillLevel = level
        if "interests" in body:
            interests = body["interests"]
            if not isinstance(interests, list):
                raise HTTPException(400, "interests must be a list")
            user.interests = interests
        if body.get("completed"):
            user.onboardingCompletedAt = datetime.now(timezone.utc).isoformat()
        session.add(user)
        session.commit()
        session.refresh(user)
        return user_public(user)


app.mount("/static", StaticFiles(directory="static"), name="static")
