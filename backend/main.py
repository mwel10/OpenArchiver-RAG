import os
import re
import email
import asyncio
import hashlib
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

import asyncpg
import meilisearch
from anthropic import Anthropic
import openai
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# ── Config ────────────────────────────────────────────────────────────────────
MEILI_URL     = os.getenv("MEILISEARCH_URL", "http://OpenArchiver-MEILI:7700")
MEILI_KEY     = os.getenv("MEILISEARCH_MASTER_KEY", "")
POSTGRES_URL  = os.getenv("DATABASE_URL", "")
QDRANT_URL    = os.getenv("QDRANT_URL", "http://qdrant:6333")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
STORAGE_ROOT  = os.getenv("STORAGE_ROOT", "/var/data/open-archiver")
EMBED_MODEL   = "text-embedding-3-small"
CLAUDE_MODEL  = "claude-sonnet-4-6"
COLLECTION    = "email_embeddings"
VECTOR_DIM    = 1536
BATCH_SIZE    = 50

# ── Clients ───────────────────────────────────────────────────────────────────
meili         = meilisearch.Client(MEILI_URL, MEILI_KEY)
qdrant        = QdrantClient(url=QDRANT_URL)
claude_client = Anthropic(api_key=ANTHROPIC_KEY)
openai_client = openai.OpenAI(api_key=OPENAI_KEY)

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
        print("[startup] Qdrant collection created")
    else:
        print("[startup] Qdrant collection already exists")
    yield

app = FastAPI(title="OpenArchiver RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Schemas ───────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    question:    str
    mode:        str = "hybrid"   # "keyword" | "semantic" | "hybrid"
    max_results: int = 15
    date_from:   Optional[str] = None
    date_to:     Optional[str] = None
    sender:      Optional[str] = None

class IndexRequest(BaseModel):
    limit:  int = 500
    offset: int = 0

# ── State ─────────────────────────────────────────────────────────────────────
_state = {"running": False, "indexed": 0, "skipped": 0, "total": 0, "error": None}

# ── Helpers ───────────────────────────────────────────────────────────────────
def to_point_id(uid: str) -> int:
    """Convert UUID string to stable integer ID for Qdrant."""
    return int(hashlib.md5(str(uid).encode()).hexdigest()[:8], 16)

def embed(texts: List[str]) -> List[List[float]]:
    """Generate embeddings via OpenAI text-embedding-3-small."""
    resp = openai_client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in resp.data]

def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()

def read_eml_body(storage_path: str) -> str:
    """Read plain text body from a .eml file stored on disk."""
    full_path = os.path.join(STORAGE_ROOT, storage_path)
    if not os.path.exists(full_path):
        return ""
    try:
        with open(full_path, "rb") as f:
            msg = email.message_from_bytes(f.read())
        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    parts.append(part.get_payload(decode=True).decode("utf-8", errors="ignore"))
                elif ct == "text/html" and not parts:
                    parts.append(strip_html(part.get_payload(decode=True).decode("utf-8", errors="ignore")))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode("utf-8", errors="ignore")
                if msg.get_content_type() == "text/html":
                    text = strip_html(text)
                parts.append(text)
        return " ".join(parts)[:5000]
    except Exception as e:
        print(f"[eml] Error reading {full_path}: {e}")
        return ""

# ── Indexing ──────────────────────────────────────────────────────────────────
async def run_indexing(limit: int, offset: int):
    """
    Fetch emails from OpenArchiver's archived_emails table, read their .eml
    bodies from disk, generate embeddings and upsert into Qdrant.
    Already-indexed emails are skipped (idempotent).
    """
    _state.update({"running": True, "indexed": 0, "skipped": 0, "error": None})
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        rows = await conn.fetch(
            "SELECT id::text, subject, sender_email, sender_name, sent_at, storage_path "
            "FROM archived_emails ORDER BY sent_at DESC LIMIT $1 OFFSET $2",
            limit, offset
        )
        _state["total"] = len(rows)

        # Check which emails are already indexed in Qdrant
        all_ids = [to_point_id(r["id"]) for r in rows]
        already = set()
        for i in range(0, len(all_ids), 100):
            results = qdrant.retrieve(
                collection_name=COLLECTION,
                ids=all_ids[i:i+100],
                with_payload=False,
                with_vectors=False,
            )
            already.update(r.id for r in results)

        to_index = [r for r in rows if to_point_id(r["id"]) not in already]
        _state["skipped"] = len(already)
        print(f"[indexing] {len(rows)} fetched, {len(already)} already indexed, {len(to_index)} new")

        for i in range(0, len(to_index), BATCH_SIZE):
            batch = to_index[i:i+BATCH_SIZE]
            texts = []
            for r in batch:
                body = read_eml_body(r["storage_path"])
                texts.append(
                    f"Subject: {r['subject'] or ''}\n"
                    f"From: {r['sender_name'] or ''} <{r['sender_email'] or ''}>\n"
                    f"Date: {r['sent_at']}\n"
                    f"Body: {body[:3000]}"
                )
            vectors = embed(texts)
            points = [
                PointStruct(
                    id=to_point_id(r["id"]),
                    vector=vec,
                    payload={
                        "email_id":     r["id"],
                        "subject":      r["subject"] or "",
                        "sender":       f"{r['sender_name'] or ''} <{r['sender_email'] or ''}>",
                        "date":         r["sent_at"].isoformat() if r["sent_at"] else "",
                        "storage_path": r["storage_path"] or "",
                    }
                )
                for r, vec in zip(batch, vectors)
            ]
            qdrant.upsert(collection_name=COLLECTION, points=points)
            _state["indexed"] += len(batch)
            print(f"[indexing] {_state['indexed']}/{len(to_index)}")
            await asyncio.sleep(0.05)

    except Exception as e:
        _state["error"] = str(e)
        print(f"[indexing] ERROR: {e}")
    finally:
        await conn.close()
        _state["running"] = False

@app.post("/index")
async def start_indexing(req: IndexRequest, background_tasks: BackgroundTasks):
    if _state["running"]:
        return {"message": "Already running", "state": _state}
    background_tasks.add_task(run_indexing, req.limit, req.offset)
    return {"message": "Indexing started", "state": _state}

@app.get("/index/status")
async def index_status():
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        total_pg = await conn.fetchval("SELECT COUNT(*) FROM archived_emails")
    finally:
        await conn.close()
    qdrant_count = qdrant.count(collection_name=COLLECTION).count
    return {
        "state":          _state,
        "qdrant_indexed": qdrant_count,
        "postgres_total": int(total_pg),
        "coverage_pct":   round(qdrant_count / max(int(total_pg), 1) * 100, 1),
    }

# ── Search ────────────────────────────────────────────────────────────────────
@app.post("/search")
async def search(req: SearchRequest):
    keyword_hits  = []
    semantic_hits = []

    # 1. Keyword search via Meilisearch (already populated by OpenArchiver)
    if req.mode in ("keyword", "hybrid"):
        try:
            for index_name in ["archived_emails", "emails"]:
                try:
                    result = meili.index(index_name).search(req.question, {
                        "limit": req.max_results,
                        "attributesToHighlight": ["*"],
                        "highlightPreTag":  "<mark>",
                        "highlightPostTag": "</mark>",
                    })
                    for hit in result.get("hits", []):
                        keyword_hits.append({
                            "id":           str(hit.get("id", "")),
                            "subject":      hit.get("subject", ""),
                            "sender":       hit.get("sender_email", ""),
                            "date":         hit.get("sent_at", hit.get("sentAt", "")),
                            "preview":      str(hit.get("subject", ""))[:400],
                            "score":        hit.get("_rankingScore", 0.5),
                            "source":       "keyword",
                            "storage_path": hit.get("storage_path", ""),
                        })
                    if keyword_hits:
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"[meili] {e}")

    # 2. Semantic search via Qdrant
    if req.mode in ("semantic", "hybrid"):
        try:
            query_vec = embed([req.question[:8000]])[0]
            sem_results = qdrant.search(
                collection_name=COLLECTION,
                query_vector=query_vec,
                limit=req.max_results,
                score_threshold=0.30,
            )
            sem_scores = {r.payload["email_id"]: r.score for r in sem_results}
            if sem_scores:
                conn = await asyncpg.connect(POSTGRES_URL)
                try:
                    rows = await conn.fetch(
                        "SELECT id::text, subject, sender_email, sender_name, sent_at, storage_path "
                        "FROM archived_emails WHERE id::text = ANY($1)",
                        list(sem_scores.keys())
                    )
                    for row in rows:
                        eid = str(row["id"])
                        semantic_hits.append({
                            "id":           eid,
                            "subject":      row["subject"] or "",
                            "sender":       f"{row['sender_name'] or ''} <{row['sender_email'] or ''}>",
                            "date":         row["sent_at"].isoformat() if row["sent_at"] else "",
                            "preview":      (row["subject"] or "")[:400],
                            "score":        sem_scores.get(eid, 0),
                            "source":       "semantic",
                            "storage_path": row["storage_path"] or "",
                        })
                finally:
                    await conn.close()
        except Exception as e:
            print(f"[qdrant] {e}")

    # 3. Merge and deduplicate by score
    seen, merged = set(), []
    for h in sorted(keyword_hits + semantic_hits, key=lambda x: x["score"], reverse=True):
        if h["id"] not in seen:
            seen.add(h["id"])
            merged.append(h)
        if len(merged) >= req.max_results:
            break

    if not merged:
        return {
            "answer": "No emails found matching your query.",
            "hits": [], "total": 0, "keyword_count": 0, "semantic_count": 0
        }

    # 4. Read full email bodies from disk for Claude context
    context_parts = []
    for i, h in enumerate(merged[:10], 1):
        body = read_eml_body(h.get("storage_path", "")) if h.get("storage_path") else ""
        context_parts.append(
            f"[{i}] From: {h['sender']} | Date: {h['date']}\n"
            f"Subject: {h['subject']}\n"
            f"Body: {body[:2000] if body else '(no body available)'}"
        )
    context = "\n---\n".join(context_parts)

    # 5. Ask Claude to analyse and summarise
    resp = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=(
            "You are an assistant helping to search and analyse an email archive. "
            "Answer the question solely based on the provided emails. "
            "Reference specific emails with [number]. "
            "For legal or business analyses: be concrete, quote relevant passages, "
            "and provide a clear conclusion. "
            "Reply in the same language as the question."
        ),
        messages=[{
            "role": "user",
            "content": f"Found emails:\n\n{context}\n\nQuestion: {req.question}"
        }]
    )

    return {
        "answer":         resp.content[0].text,
        "hits":           merged,
        "total":          len(merged),
        "keyword_count":  len(keyword_hits),
        "semantic_count": len(semantic_hits),
    }

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
