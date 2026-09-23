# OpenArchiver RAG — Semantic Email Search with Claude AI

A self-hosted RAG (Retrieval-Augmented Generation) layer that adds **hybrid semantic search** and **Claude AI analysis** on top of an existing [OpenArchiver](https://github.com/LogicLabs-OU/OpenArchiver) installation.

Ask questions like:
> *"We have a dispute with the contractor about who is responsible for the bathroom renovation. Analyse the correspondence and indicate who is responsible and why."*

Claude reads the actual `.eml` file content and gives a cited, reasoned answer.

---

## How it works

```
Browser
    │  https://<your-tailnet-host>/zoek/        (tailnet only, HTTPS)
    ▼
Tailscale Serve
    │
    ▼
Nginx  ──auth_request──►  Authelia  (session cookie, 2FA; no session → portal)
    │
    ├── /          → static frontend
    └── /api/*     → FastAPI RAG Backend
                        ├── Meilisearch  ← populated by OpenArchiver → keyword search
                        ├── PostgreSQL   ← populated by OpenArchiver → metadata + .eml paths
                        ├── Qdrant       ← built once by this stack  → semantic search
                        └── Anthropic Claude API                     → AI analysis
```

Both container ports bind to `127.0.0.1` only. Nothing on the LAN can reach the
UI or the API; the single way in is Tailscale, and behind it Authelia.

**No re-indexing of existing data.** OpenArchiver's Meilisearch and PostgreSQL are reused as-is. Only the Qdrant vector index is new and needs to be built once (incrementally, skipping already-indexed emails on re-runs).

---

## Security

The archive behind this app is personal email — often tens of thousands of
messages written by other people who never consented to anything. Treat it
accordingly.

**Access.** The UI and the API are published on `127.0.0.1` only and are reached
through Tailscale Serve, so they are visible inside your tailnet and nowhere
else. Nginx sends every request to Authelia first via `auth_request`; without a
valid session the visitor is redirected to the Authelia portal. The API sits
behind the same check as the page, so `/api/search` cannot be called without
logging in either.

An earlier version of this stack published both ports on `0.0.0.0` with no
authentication at all. If you are running that version, anyone on your LAN can
search your entire mail archive and spend your API keys. Fix that first.

**Secrets.** `docker-compose.yml` in this repository contains placeholders. Do
not commit real keys. Prefer an `.env` file with mode `600` next to the compose
file, or Docker secrets, over inline values — inline values end up in
`docker inspect`, in backups, and in every copy of the file.

**Qdrant** runs without an API key in this setup and is reachable by any
container on the same Docker network. Set `QDRANT__SERVICE__API_KEY` and pass it
from the backend if that matters in your environment.

---

## Prerequisites

- Docker + Docker Compose
- A running OpenArchiver instance
- [Tailscale](https://tailscale.com/) on the host, for HTTPS and tailnet-only access
- An [Authelia](https://www.authelia.com/) instance, for the login in front of the app
- An [Anthropic API key](https://console.anthropic.com/settings/keys) (Claude)
- An [OpenAI API key](https://platform.openai.com/api-keys) (for `text-embedding-3-small` embeddings — ~$1–2 one-time cost for 50,000 emails)

---

## Installation

### 1. Find your OpenArchiver values

**Docker network name:**
```bash
docker network ls
# Look for something like: openarchiver_default
```

**PostgreSQL credentials:**
```bash
docker inspect OpenArchiver-DB | grep -E "POSTGRES_"
# Returns: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
```

**Meilisearch master key:**
```bash
docker inspect OpenArchiver-MEILI | grep MEILI_MASTER_KEY
```

**Email storage path on host:**
```bash
docker inspect OpenArchiver-WEB | grep -A3 '"Mounts"'
# "Source" is the host path — e.g. /volume1/docker/openarchiver/data
```

### 2. Edit `docker-compose.yml`

Fill in the placeholders:

| Placeholder | Replace with |
|-------------|-------------|
| `YOUR_MEILI_MASTER_KEY_HERE` | Value from `MEILI_MASTER_KEY` |
| `YOUR_PG_USER` | Value from `POSTGRES_USER` |
| `YOUR_PG_PASSWORD` | Value from `POSTGRES_PASSWORD` |
| `YOUR_PG_DB` | Value from `POSTGRES_DB` |
| `YOUR_ANTHROPIC_API_KEY_HERE` | Your Anthropic API key |
| `YOUR_OPENAI_API_KEY_HERE` | Your OpenAI API key |
| `/path/to/openarchiver/data` | Host path to OpenArchiver email storage |
| `openarchiver_default` | Your actual Docker network name |

Also verify the container hostnames match yours:
- `OpenArchiver-DB` — PostgreSQL container name
- `OpenArchiver-MEILI` — Meilisearch container name

Check with: `docker ps --format "{{.Names}}"`

### 3. Point nginx and Authelia at your host

In `nginx/00-host.conf`, replace `YOUR_TAILNET_HOST_HERE` with your tailnet
hostname (`tailscale status` shows it). In `docker-compose.yml`, replace
`YOUR_AUTHELIA_NETWORK_HERE` with the Docker network your Authelia container
runs on.

Nginx reaches Authelia at `http://authelia:9091/auth/api/authz/auth-request`.
Adjust the hostname, port and path prefix in `nginx/default.conf` if your
Authelia is mounted differently. Confirm it answers before going further:

```bash
docker run --rm --network <your-authelia-network> curlimages/curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'X-Original-Method: GET' \
  -H 'X-Original-URL: https://<your-tailnet-host>/zoek/' \
  http://authelia:9091/auth/api/authz/auth-request
# 401 is correct here: reachable, and you are not logged in.
```

No Authelia configuration change is needed when the app shares a hostname with
an app Authelia already protects, because the session cookie is scoped to that
domain. A different hostname needs its own cookie entry and access control rule.

### 4. Start the stack

```bash
cd email-rag
docker compose up -d --build
```

Check all three containers are running:
```bash
docker ps | grep -E "qdrant|email-rag"
```

Then publish it on your tailnet (needs root):

```bash
sudo tailscale serve --bg --set-path /zoek http://127.0.0.1:8090
tailscale serve status
```

### 5. Build the semantic index

**Option A — Web UI** (easiest):
Open `https://<your-tailnet-host>/zoek/` and click **"Index new emails"**.
This indexes 500 at a time. For large archives, use Option B.

**Option B — Bulk script** (recommended for large archives):
```bash
chmod +x index_all.sh

# Auto-detect total and run:
nohup ./index_all.sh >> indexing.log 2>&1 &

# Monitor progress:
tail -f indexing.log
```

All settings are environment variables, not positional arguments:

| Variable | Default | Meaning |
|---|---|---|
| `API_URL` | `http://localhost:8001` | Backend address |
| `BATCH` | `1000` | Emails per page |
| `SLEEP_NEW` | `30` | Pause after a page that embedded something |
| `SLEEP_EMPTY` | `2` | Pause after a page with nothing new |
| `PAGE_TIMEOUT` | `1800` | Maximum duration of one page |
| `MAX_RETRIES` | `5` | Consecutive failures before giving up |
| `MAX_PAGES` | `0` | `0` is unlimited; use a small number for a dry run |
| `LOCK` | `/run/email-rag-index.lock` | Lock file; needs root, so a manual run wants `LOCK=$HOME/...` |

```bash
BATCH=500 SLEEP_NEW=60 ./index_all.sh
MAX_PAGES=3 LOCK=$HOME/idx.lock ./index_all.sh    # dry run, three pages
```

The script walks the archive with a cursor on the primary key and asks the
backend, per page, to embed only what Qdrant does not already have.

The script is **idempotent** — already-indexed emails are skipped automatically. Safe to re-run after interruption.

---

## Usage

Open `https://<your-tailnet-host>/zoek/` and sign in through Authelia.

### Search modes

| Mode | How | Use when |
|------|-----|----------|
| **Hybrid** | Keyword + semantic combined (recommended) | General use |
| **Semantic** | Meaning-based vector search | Conceptual questions, disputes, analysis |
| **Keyword** | Full-text search via Meilisearch | Exact names, reference numbers, dates |

### Example queries

- `"invoice from supplier ABC in March 2024"`
- `"who agreed to the contract extension?"`
- `"analyse the correspondence about the bathroom renovation and determine responsibility"`
- `"all emails about project delays"`

Results show the Claude AI analysis at the top, followed by the matching emails with relevance scores. Clicking an email opens it in OpenArchiver.

---

## Keeping the index up to date

New emails imported by OpenArchiver are **not automatically embedded**. Options:

**Manual:** Click "Index new emails" in the UI periodically.

**Cron job** (runs nightly at 03:00). Use the script rather than a single
`curl`: one POST only does one page, and the archive needs a full cursor pass to
pick up mail that arrived with a UUID earlier than the last cursor position.

```bash
sudo crontab -e
# Add:
0 3 * * * /path/to/email-rag/index_all.sh >> /var/log/rag-bulk.log 2>&1
```

**Or use the bulk script** to catch up after a large import:
```bash
nohup ./index_all.sh >> indexing.log 2>&1 &
```

---

## File structure

```
email-rag/
├── docker-compose.yml       ← Main config — fill in your credentials
├── index_all.sh             ← Bulk indexing script
├── backend/
│   ├── main.py              ← FastAPI: hybrid search + Claude analysis
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── index.html           ← Web UI (single file, no build step)
└── nginx/
    ├── 00-host.conf         ← Your tailnet hostname, in one place
    └── default.conf         ← Static files, /api proxy, Authelia auth_request
```

---

## Cost estimate

| Item | Cost |
|------|------|
| One-time embedding (OpenAI `text-embedding-3-small`, 50k emails) | ~$1–2 |
| Per search query (Claude Sonnet) | ~$0.003–0.008 |
| Qdrant storage (self-hosted) | $0 |

---

## Upgrading from a version before September 2026

Two things changed in a way that affects an existing Qdrant collection.

**Point IDs are now the email UUID.** They used to be
`int(md5(email_id)[:8], 16)` — 32 bits, about 4.3 billion values. That sounds
like plenty until you apply the birthday problem: at 1.15 million emails you
expect roughly 154 collisions, and a measured archive had 135. Colliding emails
overwrite each other in Qdrant, so those emails were unfindable and stayed that
way no matter how often you re-indexed, while the progress bar cheerfully read
100%. Worse, the count grows with the *square* of the archive, so it gets
quietly worse as you add mail.

**The collection name now contains the model and the dimension**
(`email_embeddings__text-embedding-3-small__1536`). Vectors from two different
models belong in two different spaces, and mixing them fails silently with worse
answers rather than with an error. A dimension check alone does not catch it:
`text-embedding-3-small` and `text-embedding-ada-002` are both 1536-dimensional.
With the model in the name, mixing cannot happen — a different model points at a
different collection, which starts empty and fills itself.

You do **not** need to re-embed. Copy the existing vectors into the new
collection under their real UUID, taking the new ID from each point's own
payload so a vector cannot end up attached to the wrong email:

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

SRC = "email_embeddings"
DST = "email_embeddings__text-embedding-3-small__1536"
q = QdrantClient(url="http://qdrant:6333", timeout=300)

if DST not in [c.name for c in q.get_collections().collections]:
    q.create_collection(DST, vectors_config=VectorParams(size=1536, distance=Distance.COSINE))

offset = None
while True:
    pts, offset = q.scroll(SRC, limit=500, offset=offset, with_vectors=True, with_payload=True)
    if not pts:
        break
    q.upsert(DST, points=[
        PointStruct(id=str(p.payload["email_id"]), vector=p.vector, payload=p.payload)
        for p in pts if (p.payload or {}).get("email_id")
    ])
    if offset is None:
        break
```

Afterwards run `index_all.sh` once: the emails that lost a collision never had a
vector, so they are embedded now. Keep the old collection until you have
compared counts, then delete it — it is a second full copy of your mail, and
anything you delete later will not be deleted from it.

**Raise Qdrant's file descriptor limit before you start.** Two collections of
this size exceed Docker's default soft limit of 1024 and Qdrant then fails to
flush segments with `Too many open files (os error 24)`, refuses connections,
and leaves the new collection in status `red`. The compose file in this
repository sets `nofile` to 65536 for that reason.

---

## Troubleshooting

**Backend logs:**
```bash
docker logs email-rag-backend --tail 50
```

**Test database connection:**
```bash
docker exec OpenArchiver-DB psql -U YOUR_PG_USER -d YOUR_PG_DB -c "SELECT COUNT(*) FROM archived_emails"
```

**Test Meilisearch connection:**
```bash
curl http://localhost:7700/health
# or from inside the backend container:
docker exec email-rag-backend curl -s http://OpenArchiver-MEILI:7700/health
```

**Network not found:**
```bash
docker network ls | grep openarchiver
docker network inspect openarchiver_default
```

**Qdrant status `red`, or `Too many open files` in `docker logs qdrant`:**
The container's file descriptor limit is too low. Check it with
`docker inspect qdrant --format '{{json .HostConfig.Ulimits}}'`; it should show
65536, not `null`. After changing it, `docker compose up -d qdrant` recreates
the container. Reloading a collection of a million points takes a few minutes,
during which search returns no results.

**Redirect loop to the Authelia portal, or a 401 that never resolves:**
The `X-Original-URL` nginx sends must be the *external* URL as the browser sees
it, including the path prefix Tailscale Serve strips before nginx. Check with
`docker logs email-rag-frontend` and Authelia's own log.

**Permission denied on frontend:**
```bash
chmod -R 755 ./frontend/
docker restart email-rag-frontend
```

**CORS errors in browser:**
These are caused by the backend returning 500. Check `docker logs email-rag-backend` first — fix the backend error and CORS will resolve automatically.

---

## Notes for OpenArchiver developers

This add-on connects to OpenArchiver's existing infrastructure without modifying it:

- **PostgreSQL:** reads `archived_emails` table (columns: `id`, `subject`, `sender_email`, `sender_name`, `sent_at`, `storage_path`)
- **Meilisearch:** reads the `archived_emails` index (read-only)
- **File storage:** mounts the `.eml` storage directory read-only
- **No writes** to any OpenArchiver database or index

Tested against OpenArchiver v0.5.0 with PostgreSQL schema as of June 2026.

A potential native integration point would be a webhook or BullMQ event fired after email ingestion, which could trigger automatic embedding of new emails without polling.
