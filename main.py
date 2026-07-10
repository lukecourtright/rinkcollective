import json
import os
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional

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

ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

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


class AdminActivity(SQLModel, table=True):
    # Server-generated audit trail behind the Admin Console's Overview
    # "recent activity" card and the Activity Log section. kind drives icon
    # + color in the frontend: "user" | "photo" | "reject" | "rink".
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


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def user_public(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.displayName,
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


@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)
    ensure_new_columns()
    sync_rinks_from_file()
    sync_equipment_from_file()
    sync_guides_from_file()


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


@app.get("/api/rinks")
def get_rinks():
    with Session(engine) as session:
        rinks = session.exec(select(Rink)).all()
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


@app.post("/api/auth/signup")
async def signup(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    displayName = body.get("displayName", "").strip()
    if not email or not displayName or len(password) < 8:
        raise HTTPException(400, "Email, display name, and a password of at least 8 characters are required")
    with Session(engine) as session:
        if session.exec(select(User).where(User.email == email)).first():
            raise HTTPException(409, "Email already registered")
        user = User(email=email, passwordHash=hash_password(password), displayName=displayName, loginCount=1)
        session.add(user)
        session.commit()
        session.refresh(user)
        log_activity(session, "user", f"{user.displayName} joined")
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
        session.add(user)
        session.commit()
        request.session["user_id"] = user.id
        return user_public(user)


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


app.mount("/static", StaticFiles(directory="static"), name="static")
