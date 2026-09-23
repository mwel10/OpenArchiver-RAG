import os
import re
import email
import time
import uuid
import asyncio
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

import asyncpg
import meilisearch
from anthropic import Anthropic
import openai
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# ── Config ────────────────────────────────────────────────────────────────────
MEILI_URL      = os.getenv("MEILISEARCH_URL", "http://OpenArchiver-MEILI:7700")
MEILI_KEY      = os.getenv("MEILISEARCH_MASTER_KEY", "")
POSTGRES_URL   = os.getenv("DATABASE_URL", "")
QDRANT_URL     = os.getenv("QDRANT_URL", "http://qdrant:6333")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY", "")
STORAGE_ROOT   = os.getenv("STORAGE_ROOT", "/var/data/open-archiver")
EMBED_MODEL    = "text-embedding-3-small"
CLAUDE_MODEL   = "claude-sonnet-4-6"
VECTOR_DIM     = 1536

# De collectienaam bevat het embeddingmodel en het aantal dimensies. Dat is geen
# opsmuk: vectoren van twee verschillende modellen horen in twee verschillende
# ruimtes, en ze mengen faalt stil met slechtere antwoorden in plaats van met een
# foutmelding. Een dimensiecontrole alleen is niet genoeg, want
# text-embedding-3-small en text-embedding-ada-002 zijn allebei 1536-dimensionaal.
# Door het model in de naam te zetten kan mengen niet: een ander model wijst naar
# een andere collectie, die leeg begint en zichzelf opnieuw vult.
# De punt-ID is sinds 2026-09-23 de volledige UUID, zie to_point_id().
COLLECTION     = "email_embeddings__%s__%d" % (EMBED_MODEL, VECTOR_DIM)
BATCH_SIZE     = 50

# Bovengrenzen voor velden die de afzender van een e-mail zelf bepaalt. Zonder
# grens kan één e-mail met een reusachtig onderwerp de embedding-aanroep laten
# falen, en daarmee elke nacht dezelfde pagina.
MAX_SUBJECT_CHARS = 500
MAX_SENDER_CHARS  = 300
MAX_BODY_CHARS    = 3000

# Hoe lang een claim op "running" mag bestaan zonder dat de achtergrondtaak
# daadwerkelijk is gestart, voordat een nieuwe aanroep hem mag overnemen.
CLAIM_TIMEOUT_SECONDS = 120

# Minimale tijd tussen twee runs in de modus "nieuwste" (de knop in de
# webinterface). Die modus is nog een volledige tabelscan, en de API heeft geen
# authenticatie: zonder deze rem kan elke pagina op het LAN hem achter elkaar
# laten draaien en de nachtelijke indexering verdringen.
NEWEST_COOLDOWN_SECONDS = 300

meili         = meilisearch.Client(MEILI_URL, MEILI_KEY)
qdrant        = QdrantClient(url=QDRANT_URL, timeout=120)
claude_client = Anthropic(api_key=ANTHROPIC_KEY)
# embed() is een blokkerende aanroep binnen async code. Zonder time-out kan een
# hangende OpenAI-verbinding ook /health, /search en /index/state bevriezen.
openai_client = openai.OpenAI(api_key=OPENAI_KEY, timeout=30.0)

@asynccontextmanager
async def lifespan(app):
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
        print("[startup] Qdrant collection %s aangemaakt" % COLLECTION)
    else:
        # Tweede slot naast de naam zelf. De naam houdt twee modellen uit elkaar;
        # deze controle vangt een collectie die ooit met andere parameters is
        # aangemaakt. Hard falen bij het opstarten is hier beter dan doordraaien,
        # want de fout is anders alleen zichtbaar als "de antwoorden zijn minder
        # goed geworden", en dat merkt niemand op tijd.
        cfg = qdrant.get_collection(COLLECTION).config.params.vectors
        if cfg.size != VECTOR_DIM or cfg.distance != Distance.COSINE:
            raise RuntimeError(
                "Collectie %s heeft size=%s distance=%s, maar deze applicatie "
                "verwacht size=%s distance=%s. Gestopt om te voorkomen dat er "
                "vectoren uit twee verschillende ruimtes door elkaar lopen."
                % (COLLECTION, cfg.size, cfg.distance, VECTOR_DIM, Distance.COSINE)
            )
        print("[startup] Qdrant collection %s bestaat al (size=%s, %s)"
              % (COLLECTION, cfg.size, cfg.distance))
    yield

app = FastAPI(title="Email RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class SearchRequest(BaseModel):
    question:    str
    mode:        str = "hybrid"
    max_results: int = 15
    date_from:   Optional[str] = None
    date_to:     Optional[str] = None
    sender:      Optional[str] = None

class IndexRequest(BaseModel):
    limit:    int = Field(500, ge=1, le=2000)
    # Twee manieren van aanroepen:
    # - met after_id, of zonder offset: cursor over het hele archief (index_all.sh);
    # - met offset (de knop in de webinterface stuurt offset: 0): de nieuwste
    #   e-mails, zoals de knop altijd deed. De waarde van offset zelf wordt niet
    #   meer gebruikt.
    offset:   Optional[int] = Field(None, ge=0)
    after_id: Optional[str] = None

    @field_validator("after_id")
    @classmethod
    def _valid_uuid(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        return str(uuid.UUID(v))

_state = {
    "running": False, "indexed": 0, "skipped": 0, "failed": 0, "total": 0, "error": None,
    # run_id begint bij een tijdstempel in milliseconden in plaats van bij 0. Na
    # een herstart van de container kan een aanroeper zo nooit het resultaat van
    # een andere run met hetzelfde nummer voor het zijne aanzien.
    "run_id": int(time.time() * 1000),
    "mode": None, "after_id": None, "next_after_id": None, "done": False,
    "claimed_at": None, "task_started": False, "newest_started_at": None,
}

def to_point_id(uid: str) -> str:
    """Het punt-ID in qdrant is de volledige UUID van de e-mail.

    Dit was `int(md5(uid)[:8], 16)`: 32 bits, dus ruim vier miljard waarden.
    Bij 1,15 miljoen e-mails levert het verjaardagsprobleem dan ongeveer 154
    botsingen op, en gemeten waren het er 135. Botsende e-mails overschrijven
    elkaar in qdrant, dus die waren onvindbaar en bleven dat, hoe vaak er ook
    opnieuw werd geindexeerd. Erger nog: het aantal groeit met het kwadraat
    van het archief, terwijl de teller in de webinterface 100% bleef melden.
    Qdrant accepteert een UUID rechtstreeks als punt-ID, dus botsen kan niet
    meer. 2026-09-23.
    """
    # uuid.UUID() dwingt de canonieke vorm af (kleine letters, met streepjes)
    # en gooit ValueError op alles wat geen UUID is. Dat is hier wezenlijk:
    # `already` wordt gevuld met wat qdrant teruggeeft en daarmee vergeleken.
    # Zou de ene kant een andere schrijfwijze hebben, dan is niets ooit gelijk,
    # ziet de indexer elke e-mail als nieuw, en wordt het hele archief opnieuw
    # ge-embed bij OpenAI. Zonder foutmelding, alleen een rekening.
    return str(uuid.UUID(str(uid)))

def embed(texts: List[str]) -> List[List[float]]:
    resp = openai_client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in resp.data]

def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()

_STORAGE_ROOT_REAL = os.path.realpath(STORAGE_ROOT)

def read_eml_body(storage_path: str) -> str:
    """Lees de tekstinhoud uit een .eml bestand."""
    full_path = os.path.realpath(os.path.join(STORAGE_ROOT, storage_path or ""))
    # os.path.join negeert STORAGE_ROOT als storage_path absoluut is, en '..'
    # kan erbuiten wijzen. Alleen bestanden binnen STORAGE_ROOT worden gelezen.
    try:
        inside = os.path.commonpath([full_path, _STORAGE_ROOT_REAL]) == _STORAGE_ROOT_REAL
    except ValueError:
        inside = False
    if not inside:
        print("[eml] pad buiten STORAGE_ROOT genegeerd")
        return ""
    if not os.path.exists(full_path):
        return ""
    try:
        with open(full_path, "rb") as f:
            msg = email.message_from_bytes(f.read())
        body_parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    body_parts.append(part.get_payload(decode=True).decode("utf-8", errors="ignore"))
                elif ct == "text/html" and not body_parts:
                    body_parts.append(strip_html(part.get_payload(decode=True).decode("utf-8", errors="ignore")))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode("utf-8", errors="ignore")
                if msg.get_content_type() == "text/html":
                    text = strip_html(text)
                body_parts.append(text)
        return " ".join(body_parts)[:5000]
    except Exception as e:
        print(f"[eml] Fout bij lezen {full_path}: {e}")
        return ""

def _short(value, limit: int) -> str:
    return (value or "")[:limit]

def _email_text(r, body: str) -> str:
    return (
        f"Onderwerp: {_short(r['subject'], MAX_SUBJECT_CHARS)}\n"
        f"Van: {_short(r['sender_name'], MAX_SENDER_CHARS)} <{_short(r['sender_email'], MAX_SENDER_CHARS)}>\n"
        f"Datum: {r['sent_at']}\n"
        f"Inhoud: {body[:MAX_BODY_CHARS]}"
    )

def _embed_batch(rows, texts):
    """Embed een batch en sla alleen e-mails over die het model zelf weigert.

    Alleen openai.BadRequestError (HTTP 400: ongeldige invoer, bijvoorbeeld te
    lang) betekent dat het aan een e-mail ligt. Dan wordt per e-mail opnieuw
    geprobeerd, en worden alleen de geweigerde e-mails overgeslagen en geteld,
    ook als dat alle e-mails van de batch zijn. Anders zou een pagina met alleen
    zo'n e-mail elke nacht opnieuw falen en de indexering daar stilzetten.

    Alle andere fouten (rate limit, time-out, verbinding, 5xx) zijn een storing
    en worden meteen doorgegeven: de pagina faalt, index_all.sh probeert later
    dezelfde pagina opnieuw, en niets wordt als "overgeslagen" weggeschreven.
    """
    try:
        return list(rows), embed(texts)
    except openai.BadRequestError as e:
        print(f"[indexing] batch van {len(texts)} geweigerd ({str(e)[:120]}); per e-mail opnieuw")
    ok_rows, ok_vectors = [], []
    for r, text in zip(rows, texts):
        try:
            ok_vectors.append(embed([text])[0])
            ok_rows.append(r)
        except openai.BadRequestError as e:
            _state["failed"] += 1
            print(f"[indexing] e-mail {r['id']} overgeslagen: {str(e)[:200]}")
    return ok_rows, ok_vectors

# ── Indexing ──────────────────────────────────────────────────────────────────
# In de cursor-modus gaat pagineren via de primaire sleutel (id > after_id
# ORDER BY id). De vorige versie deed ORDER BY sent_at DESC LIMIT/OFFSET. Op
# sent_at staat geen index, dus elke pagina was een volledige scan plus sortering
# op schijf van de hele archived_emails-tabel (1,1 miljoen rijen). Met ~1.100
# pagina's per ronde legde dat de NAS plat. De cursor is een Index Only Scan op
# archived_emails_pkey, en volledige rijen worden alleen opgehaald voor e-mails
# die nog niet in qdrant staan.
async def run_indexing(limit: int, after_id: Optional[str], mode: str):
    _state.update({
        "task_started": True, "mode": mode,
        "indexed": 0, "skipped": 0, "failed": 0, "total": 0, "error": None,
        "after_id": after_id, "next_after_id": after_id, "done": False,
    })
    conn = None
    try:
        conn = await asyncpg.connect(POSTGRES_URL)
        if mode == "newest":
            # Wat de knop in de webinterface altijd deed: de nieuwste e-mails.
            # Zonder index op sent_at is dit nog steeds een volledige tabelscan,
            # maar alleen over id en sent_at. Bedoeld voor een handmatige klik,
            # niet voor een lus.
            id_rows = await conn.fetch(
                "SELECT id FROM archived_emails ORDER BY sent_at DESC LIMIT $1",
                limit
            )
        elif after_id is None:
            id_rows = await conn.fetch(
                "SELECT id FROM archived_emails ORDER BY id LIMIT $1",
                limit
            )
        else:
            id_rows = await conn.fetch(
                "SELECT id FROM archived_emails WHERE id > $1 ORDER BY id LIMIT $2",
                uuid.UUID(after_id), limit
            )
        ids = [str(r["id"]) for r in id_rows]
        _state["total"] = len(ids)
        if mode == "newest":
            _state["done"] = True
        else:
            _state["done"] = len(ids) < limit
            if ids:
                _state["next_after_id"] = ids[-1]

        all_point_ids = [to_point_id(i) for i in ids]
        already = set()
        for i in range(0, len(all_point_ids), 100):
            results = qdrant.retrieve(collection_name=COLLECTION, ids=all_point_ids[i:i+100], with_payload=False, with_vectors=False)
            already.update(r.id for r in results)
        new_ids = [i for i in ids if to_point_id(i) not in already]
        _state["skipped"] = len(ids) - len(new_ids)
        print(f"[indexing] {len(ids)} opgehaald, {_state['skipped']} al geindexeerd, {len(new_ids)} nieuw")

        to_index = []
        if new_ids:
            to_index = await conn.fetch(
                "SELECT id::text AS id, subject, sender_email, sender_name, sent_at, storage_path "
                "FROM archived_emails WHERE id = ANY($1::uuid[])",
                [uuid.UUID(i) for i in new_ids]
            )

        for i in range(0, len(to_index), BATCH_SIZE):
            batch = to_index[i:i+BATCH_SIZE]
            texts = [_email_text(r, read_eml_body(r["storage_path"])) for r in batch]
            ok_rows, vectors = _embed_batch(batch, texts)
            points = [PointStruct(
                id=to_point_id(r["id"]),
                vector=vec,
                payload={
                    "email_id": r["id"],
                    "subject":  _short(r["subject"], MAX_SUBJECT_CHARS),
                    "sender":   f"{_short(r['sender_name'], MAX_SENDER_CHARS)} <{_short(r['sender_email'], MAX_SENDER_CHARS)}>",
                    "date":     r["sent_at"].isoformat() if r["sent_at"] else "",
                    "storage_path": r["storage_path"] or "",
                }
            ) for r, vec in zip(ok_rows, vectors)]
            if points:
                qdrant.upsert(collection_name=COLLECTION, points=points)
            _state["indexed"] += len(ok_rows)
            print(f"[indexing] {_state['indexed']}/{len(to_index)} (overgeslagen: {_state['failed']})")
            await asyncio.sleep(0.05)

    except Exception as e:
        _state["error"] = str(e)
        print(f"[indexing] FOUT: {e}")
    finally:
        if conn is not None:
            await conn.close()
        _state["running"] = False

@app.post("/index")
async def start_indexing(req: IndexRequest, background_tasks: BackgroundTasks):
    if _state["running"]:
        claimed_at = _state.get("claimed_at") or 0.0
        if _state.get("task_started") or time.time() - claimed_at < CLAIM_TIMEOUT_SECONDS:
            return {"message": "Loopt al", "state": _state}
        # De vorige claim is nooit aan een achtergrondtaak toegekomen, bijvoorbeeld
        # omdat de client wegviel voordat het antwoord verstuurd was. Zonder deze
        # uitweg bleef "running" hangen tot een herstart van de container.
        print("[indexing] verlopen claim zonder gestarte taak vrijgegeven")

    mode = "cursor" if (req.after_id is not None or req.offset is None) else "newest"
    if mode == "newest":
        since = time.time() - (_state.get("newest_started_at") or 0.0)
        if since < NEWEST_COOLDOWN_SECONDS:
            return JSONResponse(status_code=429, content={
                "message": f"Te snel na de vorige run; probeer het over {int(NEWEST_COOLDOWN_SECONDS - since)}s opnieuw",
                "state": _state,
            })
        _state["newest_started_at"] = time.time()
    # running en run_id worden hier gezet, vóór de achtergrondtaak start. Zo kan
    # een tweede aanroep er niet tussendoor glippen, en kan een aanroeper zien of
    # de toestand die hij later leest wel van zijn eigen run is.
    _state.update({"running": True, "task_started": False, "claimed_at": time.time()})
    _state["run_id"] += 1
    background_tasks.add_task(run_indexing, req.limit, req.after_id, mode)
    return {"message": "Gestart", "state": _state}

@app.get("/index/state")
async def index_state():
    """Alleen de toestand in het geheugen: geen database of qdrant, dus goedkoop om te pollen."""
    return _state

@app.get("/index/status")
async def index_status():
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        # Schatting uit de catalogus. COUNT(*) was een volledige tabelscan, en de
        # frontend vraagt deze status elke 3 seconden op zolang er geïndexeerd wordt.
        total_pg = await conn.fetchval(
            "SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = 'archived_emails'::regclass"
        )
    finally:
        await conn.close()
    qdrant_count = qdrant.count(collection_name=COLLECTION, exact=False).count
    total = int(total_pg or 0)
    # Is de tabel nooit geanalyseerd, dan is de schatting 0. Dan liever "onbekend"
    # dan een dekking van 100% die nergens op slaat.
    known = total > 0
    return {
        "state":                      _state,
        "qdrant_indexed":             qdrant_count,
        "postgres_total":             total if known else None,
        "postgres_total_is_estimate": True,
        "coverage_pct":               min(100.0, round(qdrant_count / total * 100, 1)) if known else None,
    }

# ── Search ────────────────────────────────────────────────────────────────────
@app.post("/search")
async def search(req: SearchRequest):
    keyword_hits  = []
    semantic_hits = []

    if req.mode in ("keyword", "hybrid"):
        try:
            for index_name in ["archived_emails", "emails"]:
                try:
                    result = meili.index(index_name).search(req.question, {
                        "limit": req.max_results,
                        "attributesToHighlight": ["*"],
                        "highlightPreTag": "<mark>",
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

    if req.mode in ("semantic", "hybrid"):
        try:
            query_vec = embed([req.question[:8000]])[0]
            sem_results = qdrant.search(collection_name=COLLECTION, query_vector=query_vec, limit=req.max_results, score_threshold=0.30)
            sem_scores = {r.payload["email_id"]: r.score for r in sem_results}
            sem_paths  = {r.payload["email_id"]: r.payload.get("storage_path", "") for r in sem_results}
            if sem_scores:
                # id = ANY($1::uuid[]) in plaats van id::text = ANY($1): de cast naar
                # text maakte de primaire-sleutelindex onbruikbaar, waardoor elke
                # zoekvraag de hele tabel doorliep.
                lookup_ids = []
                for eid in sem_scores.keys():
                    try:
                        lookup_ids.append(uuid.UUID(str(eid)))
                    except ValueError:
                        continue
                conn = await asyncpg.connect(POSTGRES_URL)
                try:
                    rows = await conn.fetch(
                        "SELECT id::text AS id, subject, sender_email, sender_name, sent_at, storage_path "
                        "FROM archived_emails WHERE id = ANY($1::uuid[])",
                        lookup_ids
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

    seen, merged = set(), []
    for h in sorted(keyword_hits + semantic_hits, key=lambda x: x["score"], reverse=True):
        if h["id"] not in seen:
            seen.add(h["id"])
            merged.append(h)
        if len(merged) >= req.max_results:
            break

    if not merged:
        return {"answer": "Geen e-mails gevonden.", "hits": [], "total": 0, "keyword_count": 0, "semantic_count": 0}

    # Lees de volledige body uit de .eml bestanden voor Claude
    context_parts = []
    for i, h in enumerate(merged[:10], 1):
        body = read_eml_body(h.get("storage_path", "")) if h.get("storage_path") else ""
        context_parts.append(
            f"[{i}] Van: {h['sender']} | Datum: {h['date']}\n"
            f"Onderwerp: {h['subject']}\n"
            f"Inhoud: {body[:2000] if body else '(geen inhoud beschikbaar)'}"
        )
    context = "\n---\n".join(context_parts)

    resp = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=(
            "Je bent een assistent die helpt bij het doorzoeken en analyseren van een e-mailarchief. "
            "Beantwoord de vraag uitsluitend op basis van de verstrekte e-mails. "
            "Verwijs naar specifieke e-mails met [nummer]. "
            "Bij juridische of zakelijke analyses: wees concreet, citeer relevante passages, "
            "en geef een duidelijke conclusie. "
            "Antwoord in het Nederlands tenzij de vraag in een andere taal is gesteld."
        ),
        messages=[{"role": "user", "content": f"Gevonden e-mails:\n\n{context}\n\nVraag: {req.question}"}]
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
