# OpenArchiver RAG — Semantic Email Search with Claude AI

A self-hosted RAG (Retrieval-Augmented Generation) layer that adds **hybrid semantic search** and **Claude AI analysis** on top of an existing [OpenArchiver](https://github.com/LogicLabs-OU/OpenArchiver) installation.

Ask questions like:
> *"We have a dispute with the contractor about who is responsible for the bathroom renovation. Analyse the correspondence and indicate who is responsible and why."*

Claude reads the actual `.eml` file content and gives a cited, reasoned answer.

---

## How it works

```
Browser (port 8090)
    │
    ▼
Nginx (frontend)
    │
    ▼
FastAPI RAG Backend (port 8001)
    ├── Meilisearch  ← already populated by OpenArchiver  → keyword search
    ├── PostgreSQL   ← already populated by OpenArchiver  → metadata + .eml paths
    ├── Qdrant       ← NEW, populated once by this stack  → semantic search
    └── Anthropic Claude API                              → AI analysis
```

**No re-indexing of existing data.** OpenArchiver's Meilisearch and PostgreSQL are reused as-is. Only the Qdrant vector index is new and needs to be built once (incrementally, skipping already-indexed emails on re-runs).

---

## Prerequisites

- Docker + Docker Compose
- A running OpenArchiver instance
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

### 3. Start the stack

```bash
cd email-rag
docker compose up -d --build
```

Check all three containers are running:
```bash
docker ps | grep -E "qdrant|email-rag"
```

### 4. Build the semantic index

**Option A — Web UI** (easiest):
Open `http://YOUR-NAS-IP:8090` and click **"Index new emails"**.
This indexes 500 at a time. For large archives, use Option B.

**Option B — Bulk script** (recommended for large archives):
```bash
chmod +x index_all.sh

# Auto-detect total and run:
nohup ./index_all.sh >> indexing.log 2>&1 &

# Monitor progress:
tail -f indexing.log
```

Custom parameters:
```bash
# ./index_all.sh [TOTAL] [BATCH_SIZE] [SLEEP_SECONDS] [API_URL]
nohup ./index_all.sh 233364 1000 180 http://localhost:8001 >> indexing.log 2>&1 &
```

The script is **idempotent** — already-indexed emails are skipped automatically. Safe to re-run after interruption.

---

## Usage

Open `http://YOUR-NAS-IP:8090`

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

**Cron job** (runs nightly at 03:00):
```bash
crontab -e
# Add:
0 3 * * * curl -s -X POST http://localhost:8001/index -H "Content-Type: application/json" -d '{"limit":500,"offset":0}' >> /var/log/rag-index.log
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
    └── default.conf         ← Nginx config for frontend
```

---

## Cost estimate

| Item | Cost |
|------|------|
| One-time embedding (OpenAI `text-embedding-3-small`, 50k emails) | ~$1–2 |
| Per search query (Claude Sonnet) | ~$0.003–0.008 |
| Qdrant storage (self-hosted) | $0 |

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
