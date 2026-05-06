# cinevault-backend/main.py
import os
import re
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from typing import List, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="CineVault Archive API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MONGO_URI = os.getenv("MONGO_URI", "")
TMDB_KEY  = os.getenv("TMDB_API_KEY", "")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is not set")
client = AsyncIOMotorClient(MONGO_URI)
db = client.cinevault

# ── Models ────────────────────────────────────────────────────────────────────
class User(BaseModel):
    uid: str
    email: str
    displayName: Optional[str] = None
    handle: Optional[str] = None
    photoURL: Optional[str] = None
    createdAt: datetime = Field(default_factory=datetime.utcnow)

class UserHandleUpdate(BaseModel):
    handle: str

class MovieLog(BaseModel):
    userId: str
    tmdbId: int
    title: str
    year: int
    director: str
    runtime: Optional[str] = None
    posterPath: Optional[str] = None
    rating: float = Field(ge=0.5, le=5)
    reviewText: Optional[str] = None
    tags: List[str] = []
    vibe: Optional[str] = None
    watchDate: datetime = Field(default_factory=datetime.utcnow)

class MovieLogUpdate(BaseModel):
    rating: Optional[float] = Field(default=None, ge=0.5, le=5)
    reviewText: Optional[str] = None
    tags: Optional[List[str]] = None
    vibe: Optional[str] = None
    watchDate: Optional[datetime] = None

class Category(BaseModel):
    userId: str
    name: str
    colorHex: str
    glyph: str
    filmIds: List[int] = []

class AddFilmToCategory(BaseModel):
    tmdbId: int

class WatchlistItem(BaseModel):
    userId: str
    tmdbId: int
    title: str
    year: int
    posterPath: Optional[str] = None
    addedAt: datetime = Field(default_factory=datetime.utcnow)

class FollowAction(BaseModel):
    followerId: str
    followingId: str

# ── Helpers ───────────────────────────────────────────────────────────────────
def _clean_handle(h: str) -> str:
    h = h.lstrip('@').lower().strip()
    return re.sub(r'[^a-z0-9_]', '', h)

# ── Users ─────────────────────────────────────────────────────────────────────
@app.post("/users/", status_code=201)
async def upsert_user(user: User):
    payload = user.dict()
    created = payload.pop("createdAt")
    await db.users.update_one(
        {"uid": user.uid},
        {"$set": {k: v for k, v in payload.items() if v is not None},
         "$setOnInsert": {"createdAt": created}},
        upsert=True,
    )
    return {"message": "User ready"}

@app.get("/users/{uid}")
async def get_user(uid: str):
    doc = await db.users.find_one({"uid": uid}, {"_id": 0})
    if not doc: raise HTTPException(404, "User not found")
    return doc

@app.put("/users/{uid}/handle")
async def set_handle(uid: str, body: UserHandleUpdate):
    handle = _clean_handle(body.handle)
    if len(handle) < 2: raise HTTPException(400, "Handle too short")
    if await db.users.find_one({"handle": handle, "uid": {"$ne": uid}}):
        raise HTTPException(409, "Handle already taken")
    await db.users.update_one({"uid": uid}, {"$set": {"handle": handle}})
    return {"handle": handle}

@app.get("/users/handle/{handle}")
async def get_user_by_handle(handle: str):
    doc = await db.users.find_one({"handle": _clean_handle(handle)}, {"_id": 0, "email": 0})
    if not doc: raise HTTPException(404, "User not found")
    return doc

@app.get("/users/search/{query}")
async def search_users(query: str):
    q = re.escape(_clean_handle(query))
    cursor = db.users.find(
        {"handle": {"$regex": q, "$options": "i"}},
        {"_id": 0, "email": 0, "uid": 1, "handle": 1, "displayName": 1, "photoURL": 1}
    ).limit(20)
    return await cursor.to_list(length=20)

# ── Follow ────────────────────────────────────────────────────────────────────
@app.post("/follow/", status_code=201)
async def follow_user(action: FollowAction):
    if action.followerId == action.followingId: raise HTTPException(400, "Cannot follow yourself")
    await db.follows.update_one(
        {"followerId": action.followerId, "followingId": action.followingId},
        {"$setOnInsert": {**action.dict(), "createdAt": datetime.utcnow()}},
        upsert=True,
    )
    return {"message": "Following"}

@app.delete("/follow/")
async def unfollow_user(action: FollowAction):
    await db.follows.delete_one({"followerId": action.followerId, "followingId": action.followingId})
    return {"message": "Unfollowed"}

@app.get("/follow/{uid}/following")
async def get_following(uid: str):
    cursor = db.follows.find({"followerId": uid}, {"_id": 0, "followingId": 1})
    docs = await cursor.to_list(length=500)
    return [d["followingId"] for d in docs]

@app.get("/follow/{uid}/followers")
async def get_followers(uid: str):
    cursor = db.follows.find({"followingId": uid}, {"_id": 0, "followerId": 1})
    docs = await cursor.to_list(length=500)
    return [d["followerId"] for d in docs]

@app.get("/follow/{uid}/counts")
async def get_follow_counts(uid: str):
    return {
        "following": await db.follows.count_documents({"followerId": uid}),
        "followers": await db.follows.count_documents({"followingId": uid}),
    }

# ── Feed ──────────────────────────────────────────────────────────────────────
@app.get("/feed/{uid}")
async def get_feed(uid: str, limit: int = 40):
    cursor = db.follows.find({"followerId": uid}, {"_id": 0, "followingId": 1})
    following_ids = [d["followingId"] for d in await cursor.to_list(500)]
    if not following_ids: return []

    user_docs = await db.users.find(
        {"uid": {"$in": following_ids}},
        {"_id": 0, "uid": 1, "handle": 1, "displayName": 1, "photoURL": 1}
    ).to_list(500)
    user_map = {u["uid"]: u for u in user_docs}

    logs = await db.logs.find(
        {"userId": {"$in": following_ids}},
        {"_id": 1, "userId": 1, "tmdbId": 1, "title": 1, "year": 1,
         "posterPath": 1, "rating": 1, "reviewText": 1, "vibe": 1, "watchDate": 1}
    ).sort("watchDate", -1).limit(limit).to_list(limit)
    for l in logs:
        l["_id"] = str(l["_id"])
        l["kind"] = "log"
        l["user"] = user_map.get(l["userId"], {})

    wl_items = await db.watchlist.find(
        {"userId": {"$in": following_ids}},
        {"_id": 0, "userId": 1, "tmdbId": 1, "title": 1, "year": 1, "posterPath": 1, "addedAt": 1}
    ).sort("addedAt", -1).limit(limit).to_list(limit)
    for w in wl_items:
        w["kind"] = "watchlist"
        w["watchDate"] = w.pop("addedAt")
        w["user"] = user_map.get(w["userId"], {})

    combined = logs + wl_items
    combined.sort(key=lambda x: x.get("watchDate", datetime.min), reverse=True)
    return combined[:limit]

# ── Movie Logs ────────────────────────────────────────────────────────────────
@app.post("/logs/", status_code=201)
async def log_movie(log: MovieLog):
    payload = log.dict()
    res = await db.logs.update_one(
        {"userId": log.userId, "tmdbId": log.tmdbId},
        {"$set": payload}, upsert=True,
    )
    if res.upserted_id: return {"id": str(res.upserted_id), "created": True}
    doc = await db.logs.find_one({"userId": log.userId, "tmdbId": log.tmdbId}, {"_id": 1})
    return {"id": str(doc["_id"]), "created": False}

@app.get("/logs/{user_id}")
async def get_user_logs(user_id: str, limit: int = 200):
    cursor = db.logs.find(
        {"userId": user_id},
        {"_id": 1, "tmdbId": 1, "title": 1, "year": 1, "director": 1,
         "runtime": 1, "posterPath": 1, "rating": 1, "reviewText": 1,
         "tags": 1, "vibe": 1, "watchDate": 1}
    ).sort("watchDate", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs: d["_id"] = str(d["_id"])
    return docs

@app.put("/logs/{log_id}")
async def update_log(log_id: str, patch: MovieLogUpdate):
    update = {k: v for k, v in patch.dict().items() if v is not None}
    if not update: raise HTTPException(400, "No fields to update")
    res = await db.logs.update_one({"_id": ObjectId(log_id)}, {"$set": update})
    if res.matched_count == 0: raise HTTPException(404, "Log not found")
    return {"message": "Updated"}

@app.delete("/logs/{log_id}")
async def delete_log(log_id: str):
    result = await db.logs.delete_one({"_id": ObjectId(log_id)})
    if result.deleted_count == 0: raise HTTPException(404, "Log not found")
    return {"message": "Deleted"}

# ── Watchlist ─────────────────────────────────────────────────────────────────
@app.post("/watchlist/", status_code=201)
async def add_to_watchlist(item: WatchlistItem):
    await db.watchlist.update_one(
        {"userId": item.userId, "tmdbId": item.tmdbId},
        {"$setOnInsert": item.dict()}, upsert=True,
    )
    return {"message": "Added to watchlist"}

@app.get("/watchlist/{user_id}")
async def get_watchlist(user_id: str):
    cursor = db.watchlist.find({"userId": user_id}, {"_id": 0}).sort("addedAt", -1)
    return await cursor.to_list(length=200)

@app.delete("/watchlist/{user_id}/{tmdb_id}")
async def remove_from_watchlist(user_id: str, tmdb_id: int):
    await db.watchlist.delete_one({"userId": user_id, "tmdbId": tmdb_id})
    return {"message": "Removed"}

# ── Categories ────────────────────────────────────────────────────────────────
@app.post("/categories/", status_code=201)
async def create_category(cat: Category):
    result = await db.categories.insert_one(cat.dict())
    return {"id": str(result.inserted_id)}

@app.get("/categories/{user_id}")
async def get_categories(user_id: str):
    cursor = db.categories.find(
        {"userId": user_id},
        {"_id": 1, "userId": 1, "name": 1, "colorHex": 1, "glyph": 1, "filmIds": 1}
    )
    docs = await cursor.to_list(length=50)
    for d in docs: d["_id"] = str(d["_id"])
    return docs

@app.post("/categories/{category_id}/films")
async def add_film_to_category(category_id: str, body: AddFilmToCategory):
    res = await db.categories.update_one(
        {"_id": ObjectId(category_id)}, {"$addToSet": {"filmIds": body.tmdbId}}
    )
    if res.matched_count == 0: raise HTTPException(404, "Category not found")
    return {"message": "Film added"}

@app.delete("/categories/{category_id}/films/{tmdb_id}")
async def remove_film_from_category(category_id: str, tmdb_id: int):
    res = await db.categories.update_one(
        {"_id": ObjectId(category_id)}, {"$pull": {"filmIds": tmdb_id}}
    )
    if res.matched_count == 0: raise HTTPException(404, "Category not found")
    return {"message": "Film removed"}

@app.delete("/categories/{category_id}")
async def delete_category(category_id: str):
    result = await db.categories.delete_one({"_id": ObjectId(category_id)})
    if result.deleted_count == 0: raise HTTPException(404, "Category not found")
    return {"message": "Deleted"}

# ── TMDB Proxy ────────────────────────────────────────────────────────────────
@app.get("/tmdb/search")
async def search_tmdb(q: str, page: int = 1):
    async with httpx.AsyncClient() as c:
        resp = await c.get("https://api.themoviedb.org/3/search/movie",
            params={"api_key": TMDB_KEY, "query": q, "page": page, "include_adult": False})
    return resp.json()

@app.get("/tmdb/trending")
async def get_trending():
    async with httpx.AsyncClient() as c:
        resp = await c.get("https://api.themoviedb.org/3/trending/movie/week",
            params={"api_key": TMDB_KEY})
    return resp.json()

@app.get("/tmdb/movie/{tmdb_id}")
async def get_tmdb_movie(tmdb_id: int):
    async with httpx.AsyncClient() as c:
        resp = await c.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}",
            params={"api_key": TMDB_KEY, "append_to_response": "credits"})
    return resp.json()

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.get("/stats/{user_id}")
async def get_stats(user_id: str):
    try:
        watched  = await db.logs.count_documents({"userId": user_id})
        reviewed = await db.logs.count_documents({"userId": user_id, "reviewText": {"$nin": [None, ""]}})
        watchlist = await db.watchlist.count_documents({"userId": user_id})
        pipeline = [
            {"$match": {"userId": user_id}}, {"$unwind": "$tags"},
            {"$group": {"_id": "$tags", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}, {"$limit": 1},
        ]
        top_tag = "—"
        async for doc in db.logs.aggregate(pipeline):
            top_tag = doc["_id"]; break
        return {"watched": watched, "reviewed": reviewed, "watchlist": watchlist, "topTag": top_tag}
    except Exception as e:
        raise HTTPException(500, f"Stats error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)