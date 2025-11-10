from fastapi import FastAPI, HTTPException, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, AsyncGenerator
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image
from io import BytesIO
import base64
import bcrypt
import os
import uvicorn
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
import os

# Load .env file
load_dotenv()

# Get Mongo URI
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise ValueError("MONGO_URI not found in .env file")

# --------------------------------------------
# MongoDB Config (Async)
# --------------------------------------------
client = AsyncIOMotorClient(MONGO_URI)
db = client["public"]

# --------------------------------------------
# Models
# --------------------------------------------
class Event(BaseModel):
    id: Optional[int]
    name: str
    desc: str
    votes: int = 0
    poster: Optional[str] = None


# --------------------------------------------
# Utility: Compress Image
# --------------------------------------------
def compress_image(image_bytes: bytes, quality: int = 25, max_size_kb: int = 250) -> bytes:
    """Compress an image to a small base64-safe size."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    if img.width > 1920 or img.height > 1080:
        img.thumbnail((1920, 1080))

    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=True)
    buffer.seek(0)

    while len(buffer.getvalue()) / 1024 > max_size_kb and quality > 10:
        buffer = BytesIO()
        quality -= 5
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        buffer.seek(0)

    return buffer.getvalue()


# --------------------------------------------
# Admin Setup
# --------------------------------------------
DEFAULT_ADMIN = {"username": "admin", "password": "intellexa2025"}

async def ensure_admin_exists():
    admins_col = db["admins"]
    existing = await admins_col.find_one({"username": DEFAULT_ADMIN["username"]})
    if not existing:
        hashed_pw = bcrypt.hashpw(DEFAULT_ADMIN["password"].encode("utf-8"), bcrypt.gensalt())
        await admins_col.insert_one({"username": DEFAULT_ADMIN["username"], "password": hashed_pw})
        print("✅ Default admin created.")


# --------------------------------------------
# Lifespan Event (Startup + Shutdown)
# --------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    existing_collections = await db.list_collection_names()
    if "events" not in existing_collections:
        await db.create_collection("events")
    if "admins" not in existing_collections:
        await db.create_collection("admins")

    await ensure_admin_exists()
    print("🚀 IntelLexa Event Voting API initialized.")
    yield
    client.close()
    print("🛑 MongoDB connection closed.")


# --------------------------------------------
# App Setup
# --------------------------------------------
app = FastAPI(title="IntelLexa Event Voting API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://d-alfredsam.github.io/"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------
# Helper Functions
# --------------------------------------------
def clean_doc(doc):
    """Remove MongoDB _id and make sure response is JSON safe."""
    if not doc:
        return None
    if "_id" in doc:
        del doc["_id"]
    return doc


# --------------------------------------------
# Routes
# --------------------------------------------
@app.get("/")
async def home():
    return {"status": "OK", "message": "IntelLexa Voting API Running ✅"}


# ---------- Admin Login ----------
@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    admins_col = db["admins"]
    admin = await admins_col.find_one({"username": DEFAULT_ADMIN["username"]})
    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found")

    if not bcrypt.checkpw(password.encode("utf-8"), admin["password"]):
        raise HTTPException(status_code=403, detail="Invalid admin password")

    return {"message": "Login successful"}


# ---------- Events ----------
@app.get("/events")
async def get_events():
    events_col = db["events"]
    cursor = events_col.find({})
    events = [clean_doc(e) for e in await cursor.to_list(None)]
    return events


@app.post("/events")
async def add_event(
    name: str = Form(...),
    desc: str = Form(...),
    poster: Optional[UploadFile] = File(None)
):
    events_col = db["events"]
    last_event = await events_col.find_one(sort=[("id", -1)])
    event_id = (last_event["id"] + 1) if last_event else 1

    poster_data = None
    if poster:
        original_bytes = await poster.read()
        compressed_bytes = await asyncio.to_thread(compress_image, original_bytes)
        b64_data = base64.b64encode(compressed_bytes).decode("utf-8")
        poster_data = f"data:image/jpeg;base64,{b64_data}"

    new_event = {
        "id": event_id,
        "name": name,
        "desc": desc,
        "votes": 0,
        "poster": poster_data,
    }

    await events_col.insert_one(new_event)
    return {"message": "Event added successfully", "event": clean_doc(new_event)}


@app.put("/events/{event_id}")
async def edit_event(
    event_id: int,
    name: str = Form(...),
    desc: str = Form(...),
    poster: Optional[UploadFile] = File(None)
):
    events_col = db["events"]
    event = await events_col.find_one({"id": event_id})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    update_data = {"name": name, "desc": desc}
    if poster:
        original_bytes = await poster.read()
        compressed_bytes = await asyncio.to_thread(compress_image, original_bytes)
        b64_data = base64.b64encode(compressed_bytes).decode("utf-8")
        update_data["poster"] = f"data:image/jpeg;base64,{b64_data}"

    await events_col.update_one({"id": event_id}, {"$set": update_data})
    updated_event = await events_col.find_one({"id": event_id})
    return {"message": "Event updated successfully", "event": clean_doc(updated_event)}


@app.delete("/events/{event_id}")
async def delete_event(event_id: int):
    events_col = db["events"]
    result = await events_col.delete_one({"id": event_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"message": "Event deleted successfully"}


@app.post("/vote/{event_id}")
async def vote_event(event_id: int, delta: int = Form(...)):
    events_col = db["events"]
    event = await events_col.find_one({"id": event_id})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    new_votes = max(0, event["votes"] + delta)
    await events_col.update_one({"id": event_id}, {"$set": {"votes": new_votes}})
    return {"message": "Vote updated", "new_votes": new_votes}


@app.post("/events/reset_votes")
async def reset_votes():
    events_col = db["events"]
    await events_col.update_many({}, {"$set": {"votes": 0}})
    return {"message": "All votes reset successfully"}





