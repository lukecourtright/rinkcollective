import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Optional

import bcrypt
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine, select
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")

RINKS_FILE = pathlib.Path("rinks.json")

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


class PendingRink(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    submittedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict = Field(sa_column=Column(JSON))


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


def sync_rinks_from_file():
    rinks = json.loads(RINKS_FILE.read_text(encoding="utf-8"))
    with Session(engine) as session:
        for rink in rinks:
            session.merge(Rink(**rink))
        file_ids = {r["id"] for r in rinks}
        for stale in session.exec(select(Rink).where(Rink.id.not_in(file_ids))).all():
            session.delete(stale)
        session.commit()


@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)
    sync_rinks_from_file()


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/api/rinks")
def get_rinks():
    with Session(engine) as session:
        rinks = session.exec(select(Rink)).all()
        return [rink.model_dump() for rink in rinks]


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
