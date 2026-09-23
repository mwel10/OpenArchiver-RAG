#!/usr/bin/env bash
# =============================================================================
# index_all.sh — semantische indexering van het OpenArchiver-archief (email-rag)
#
# Loopt het hele archief door met een cursor op de primaire sleutel en laat de
# backend per pagina alleen de e-mails indexeren die nog niet in qdrant staan.
#
# Gebruik (alle instellingen optioneel, via omgevingsvariabelen):
#   ./index_all.sh
#   BATCH=500 SLEEP_NEW=60 ./index_all.sh
#   MAX_PAGES=3 ./index_all.sh              # proefrun: stopt na 3 pagina's
#
# De standaardlock staat in /run en vereist root (zoals de cronjob). Voor een
# handmatige proefrun als gewone gebruiker: LOCK=$HOME/email-rag-index.lock.
#
# Wijzigingen 2026-09-14, en waarom:
#
# 1. flock. De cronjob startte elke nacht om 03:00 een nieuw exemplaar zonder
#    vergrendeling. Eén ronde duurde ~57 uur, dus liepen er drie tegelijk, die
#    samen de NAS-schijven verzadigden. Het script vergrendelt zichzelf nu, zodat
#    ook een handmatige start nooit naast de cronjob draait. De lock staat in
#    /run (alleen root mag daar schrijven), niet in /var/lock (= /run/lock, voor
#    iedereen beschrijfbaar), zodat een andere gebruiker de indexering niet kan
#    blokkeren of root een ander bestand kan laten afkappen.
#
# 2. Cursor in plaats van OFFSET. Elke OFFSET-pagina was in de backend een
#    volledige tabelscan plus sortering. De cursor (after_id) is een indexscan.
#
# 3. Geen overgeslagen pagina's meer. Bij "Loopt al" (HTTP 200) schoof de oude
#    versie de offset toch op, waardoor die pagina nooit werd geïndexeerd. Nu
#    wacht het script en probeert het dezelfde pagina opnieuw.
#
# 4. Wachten tot de pagina echt klaar is, via /index/state (goedkoop), in plaats
#    van blind 180 seconden. Pagina's zonder nieuwe e-mails krijgen een korte
#    pauze; alleen na echte indexering (OpenAI + qdrant) wordt langer gewacht.
#
# 5. Het resultaat telt alleen als het aantoonbaar van deze aanroep is: zowel
#    run_id als de cursor in de toestand moeten overeenkomen.
# =============================================================================

set -uo pipefail

API_URL="${API_URL:-http://localhost:8001}"
BATCH="${BATCH:-1000}"
SLEEP_NEW="${SLEEP_NEW:-30}"            # pauze na een pagina met nieuwe e-mails
SLEEP_EMPTY="${SLEEP_EMPTY:-2}"         # pauze na een pagina zonder nieuwe e-mails
POLL="${POLL:-3}"                       # hoe vaak /index/state wordt bekeken
PAGE_TIMEOUT="${PAGE_TIMEOUT:-1800}"    # maximale duur van één pagina
MAX_RETRIES="${MAX_RETRIES:-5}"         # opeenvolgende fouten voordat het script stopt
MAX_MISMATCHES="${MAX_MISMATCHES:-20}"  # opeenvolgende keren een vreemde toestand
MAX_PAGES="${MAX_PAGES:-0}"             # 0 = onbeperkt
RETRY_SLEEP="${RETRY_SLEEP:-60}"        # wachttijd na een fout
LOCK="${LOCK:-/run/email-rag-index.lock}"

UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# Geen voorloopnullen: bash leest "08" in een rekensom als (ongeldig) octaal.
INT_RE='^(0|[1-9][0-9]*)$'

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Waarden die uit een HTTP-antwoord komen: stuurtekens (zoals regeleinden) eruit
# en ingekort, zodat ze geen nep-logregels kunnen maken.
clean() { printf '%s' "$1" | tr -d '\000-\037\177' | cut -c1-300; }

for v in BATCH SLEEP_NEW SLEEP_EMPTY POLL PAGE_TIMEOUT MAX_RETRIES MAX_MISMATCHES MAX_PAGES RETRY_SLEEP; do
  [[ "${!v}" =~ $INT_RE ]] || { log "Ongeldige waarde voor $v: '$(clean "${!v}")' (verwacht een geheel getal zonder voorloopnul)"; exit 2; }
done
[ "$BATCH" -ge 1 ] && [ "$BATCH" -le 2000 ] || { log "BATCH moet tussen 1 en 2000 liggen"; exit 2; }
[ "$POLL" -ge 1 ] || { log "POLL moet minstens 1 zijn"; exit 2; }

# ── Vergrendeling ─────────────────────────────────────────────────────────────
if ! { : >> "$LOCK"; } 2>/dev/null; then
  log "Kan lockbestand $LOCK niet openen (standaard alleen als root; zet anders LOCK=...) — gestopt."
  exit 2
fi
exec 9>>"$LOCK"
if ! flock -n 9; then
  log "Er loopt al een indexering (lock $LOCK is bezet) — dit exemplaar stopt."
  exit 0
fi

# Wacht tot de backend geen pagina meer verwerkt. Geeft 1 terug bij time-out.
wait_idle() {
  local waited=0 running
  while :; do
    running=$(curl -s -m 20 "$API_URL/index/state" | jq -r '.running' 2>/dev/null)
    [ "$running" = "false" ] && return 0
    [ "$waited" -ge "$PAGE_TIMEOUT" ] && return 1
    sleep "$POLL"
    waited=$((waited + POLL))
  done
}

fail_or_retry() {
  RETRIES=$((RETRIES + 1))
  log "$1 (poging $RETRIES/$MAX_RETRIES, cursor ${AFTER:-begin})"
  if [ "$RETRIES" -ge "$MAX_RETRIES" ]; then
    log "Te veel opeenvolgende fouten — gestopt. Hervat later vanaf het begin; al geindexeerde e-mails worden overgeslagen."
    exit 1
  fi
  sleep "$RETRY_SLEEP"
}

log "Start — batch=$BATCH api=$API_URL"
if ! wait_idle; then
  log "Backend bleef langer dan ${PAGE_TIMEOUT}s bezig met een andere run — gestopt."
  exit 1
fi

AFTER=""
PAGE=0
RETRIES=0
MISMATCHES=0
TOTAL_SEEN=0
TOTAL_NEW=0
TOTAL_FAILED=0

while :; do
  if [ -n "$AFTER" ]; then
    BODY=$(printf '{"limit":%d,"after_id":"%s"}' "$BATCH" "$AFTER")
  else
    BODY=$(printf '{"limit":%d}' "$BATCH")
  fi

  RESP=$(curl -s -m 60 -w '\n%{http_code}' -X POST "$API_URL/index" \
    -H 'Content-Type: application/json' -d "$BODY")
  CODE=$(printf '%s\n' "$RESP" | tail -n 1)
  JSON=$(printf '%s\n' "$RESP" | sed '$d')

  if [ "$CODE" != "200" ]; then
    fail_or_retry "HTTP $(clean "${CODE:-geen antwoord}") bij het starten van een pagina"
    continue
  fi

  MSG=$(printf '%s' "$JSON" | jq -r '.message // empty' 2>/dev/null)
  if [ "$MSG" = "Loopt al" ]; then
    # Telt mee met de mismatches: zonder maximum kon een andere aanroeper die
    # steeds opnieuw runs start, dit script de lock eindeloos laten vasthouden.
    MISMATCHES=$((MISMATCHES + 1))
    log "Backend verwerkt nog een andere run — wacht en probeer dezelfde pagina opnieuw ($MISMATCHES/$MAX_MISMATCHES)."
    if [ "$MISMATCHES" -ge "$MAX_MISMATCHES" ]; then
      log "Backend is steeds door een andere run bezet — gestopt."
      exit 1
    fi
    if ! wait_idle; then
      log "Backend bleef langer dan ${PAGE_TIMEOUT}s bezig — gestopt."
      exit 1
    fi
    continue
  fi
  RUN_ID=$(printf '%s' "$JSON" | jq -r '.state.run_id // empty' 2>/dev/null)

  if ! wait_idle; then
    log "Pagina duurde langer dan ${PAGE_TIMEOUT}s — gestopt."
    exit 1
  fi

  STATE=$(curl -s -m 20 "$API_URL/index/state")
  STATE_RUN=$(printf '%s' "$STATE" | jq -r '.run_id // empty' 2>/dev/null)
  STATE_AFTER=$(printf '%s' "$STATE" | jq -r '.after_id // empty' 2>/dev/null)
  STATE_MODE=$(printf '%s' "$STATE" | jq -r '.mode // empty' 2>/dev/null)
  if [ -z "$RUN_ID" ] || [ "$STATE_RUN" != "$RUN_ID" ] || [ "$STATE_AFTER" != "$AFTER" ] || [ "$STATE_MODE" != "cursor" ]; then
    # De toestand hoort bij een andere run: iemand anders (bijvoorbeeld de knop in
    # de webinterface) startte er tussendoor een. Het resultaat van onze pagina is
    # dan niet meer leesbaar; dezelfde pagina opnieuw doen is veilig, want
    # geindexeerde e-mails worden overgeslagen.
    MISMATCHES=$((MISMATCHES + 1))
    log "Toestand hoort bij een andere run (verwacht run $(clean "${RUN_ID:-?}") cursor ${AFTER:-begin}, gezien run $(clean "${STATE_RUN:-?}") cursor $(clean "${STATE_AFTER:-begin}") modus $(clean "${STATE_MODE:-?}")) — pagina opnieuw ($MISMATCHES/$MAX_MISMATCHES)."
    if [ "$MISMATCHES" -ge "$MAX_MISMATCHES" ]; then
      log "Te vaak een vreemde toestand — gestopt."
      exit 1
    fi
    sleep "$POLL"
    continue
  fi
  MISMATCHES=0

  ERR=$(printf '%s' "$STATE" | jq -r '.error // empty' 2>/dev/null)
  if [ -n "$ERR" ]; then
    fail_or_retry "Fout in de backend: $(clean "$ERR")"
    continue
  fi

  NEXT=$(printf '%s' "$STATE" | jq -r '.next_after_id // empty')
  if [ -n "$NEXT" ] && ! [[ "$NEXT" =~ $UUID_RE ]]; then
    fail_or_retry "Backend gaf een ongeldige cursor terug"
    continue
  fi
  RETRIES=0

  SEEN=$(printf '%s' "$STATE" | jq -r '.total // 0')
  NEW=$(printf '%s' "$STATE" | jq -r '.indexed // 0')
  FAILED=$(printf '%s' "$STATE" | jq -r '.failed // 0')
  DONE=$(printf '%s' "$STATE" | jq -r '.done // false')
  [[ "$SEEN" =~ $INT_RE ]] || SEEN=0
  [[ "$NEW" =~ $INT_RE ]] || NEW=0
  [[ "$FAILED" =~ $INT_RE ]] || FAILED=0

  PAGE=$((PAGE + 1))
  TOTAL_SEEN=$((TOTAL_SEEN + SEEN))
  TOTAL_NEW=$((TOTAL_NEW + NEW))
  TOTAL_FAILED=$((TOTAL_FAILED + FAILED))
  log "Pagina $PAGE: $SEEN bekeken, $NEW nieuw geindexeerd, $FAILED overgeslagen (cursor ${NEXT:-einde})"

  if [ "$DONE" = "true" ] || [ -z "$NEXT" ]; then
    break
  fi
  if [ "$MAX_PAGES" -gt 0 ] && [ "$PAGE" -ge "$MAX_PAGES" ]; then
    log "MAX_PAGES=$MAX_PAGES bereikt — proefrun gestopt."
    break
  fi

  AFTER="$NEXT"
  if [ "$NEW" -gt 0 ]; then sleep "$SLEEP_NEW"; else sleep "$SLEEP_EMPTY"; fi
done

log "Klaar — $PAGE pagina's, $TOTAL_SEEN e-mails bekeken, $TOTAL_NEW nieuw geindexeerd, $TOTAL_FAILED overgeslagen."
