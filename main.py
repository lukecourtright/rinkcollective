import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Optional

import bcrypt
import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import JSON, Column, LargeBinary, inspect, text
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


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def user_public(user: User) -> dict:
    return {"id": user.id, "email": user.email, "displayName": user.displayName}


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
    for table_name, model in (("rink", Rink), ("equipment", Equipment)):
        if table_name not in existing_tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        with engine.begin() as conn:
            for column in model.__table__.columns:
                if column.name not in existing:
                    ddl = CreateColumn(column).compile(dialect=engine.dialect)
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))


def sync_rinks_from_file():
    rinks = json.loads(RINKS_FILE.read_text(encoding="utf-8"))
    with Session(engine) as session:
        for rink in rinks:
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


@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)
    ensure_new_columns()
    sync_rinks_from_file()
    sync_equipment_from_file()


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/equipment")
def equipment_page():
    return FileResponse("static/equipment.html")


@app.get("/api/rinks")
def get_rinks():
    with Session(engine) as session:
        rinks = session.exec(select(Rink)).all()
        return [rink.model_dump() for rink in rinks]


@app.get("/api/equipment")
def get_equipment():
    with Session(engine) as session:
        products = session.exec(select(Equipment)).all()
        return [product.model_dump() for product in products]


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


@app.get("/admin/photos")
def admin_photos_page():
    return FileResponse("static/admin.html")


@app.get("/api/admin/photos")
def list_pending_photos(request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        photos = session.exec(select(RinkPhoto).where(RinkPhoto.status == "pending")).all()
        result = []
        for photo in photos:
            rink = session.get(Rink, photo.rinkId)
            user = session.get(User, photo.userId)
            result.append({
                "id": photo.id,
                "rinkId": photo.rinkId,
                "rinkName": rink.name if rink else "(deleted rink)",
                "rinkCity": rink.city if rink else "",
                "rinkState": rink.state if rink else "",
                "caption": photo.caption,
                "submittedAt": photo.submittedAt,
                "submitterName": user.displayName if user else "(deleted user)",
                "submitterEmail": user.email if user else "",
            })
        return result


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
        require_admin(request, session)
        photo = session.get(RinkPhoto, photo_id)
        if photo is None:
            raise HTTPException(404, "Photo not found")
        photo.status = "approved"
        session.add(photo)
        session.commit()
    return {"status": "approved"}


@app.post("/api/admin/photos/{photo_id}/reject")
def reject_photo(photo_id: int, request: Request):
    with Session(engine) as session:
        require_admin(request, session)
        photo = session.get(RinkPhoto, photo_id)
        if photo is None:
            raise HTTPException(404, "Photo not found")
        session.delete(photo)
        session.commit()
    return {"status": "rejected"}


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
        user = User(email=email, passwordHash=hash_password(password), displayName=displayName)
        session.add(user)
        session.commit()
        session.refresh(user)
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
