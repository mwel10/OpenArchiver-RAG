# Vastgezet op een patchversie in plaats van op de tak. python:3.12-slim
# verschuift onder je handen; de container draaide 3.12.13 terwijl 3.12.15 de
# laatste patch van die tak was, en 3.12 krijgt alleen nog veiligheidsfixes.
FROM python:3.12.15-slim

WORKDIR /app

# Geen gcc en geen libpq-dev meer. Alles in requirements.lock levert een
# manylinux-wheel voor cp312 op amd64, en asyncpg praat zelf het
# Postgres-protocol, dus libpq wordt niet gebruikt. Bouwgereedschap in een
# productie-image is aanvalsoppervlak zonder functie.
COPY requirements.lock .

# --require-hashes: pip weigert te installeren als een pakket niet exact
# overeenkomt met de vastgelegde hash. Daarmee is "pin versions and verify
# integrity" niet langer een belofte van de registry maar een controle hier.
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

# Niet als root. De applicatie leest alleen; ze hoeft nergens te schrijven.
RUN useradd --system --no-create-home --uid 10001 emailrag \
    && chown -R emailrag:emailrag /app
USER emailrag

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
