import os
import json
import time
import threading
import http.server
import socketserver
from datetime import datetime
from urllib.parse import urlparse

import requests
import telebot


# ============================================================
# CONFIGURAZIONE
# ============================================================

SECRET_FILE = "/etc/secrets/bot_secrets.env"

RENDER_PORT = int(os.getenv("PORT", "10000"))

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"

API_TIMEOUT = 30

NUMERO_PARTITE_REPORT = 8
NUMERO_PARTITE_FORM = 10


# ============================================================
# LETTURA SECRET FILE RENDER
# ============================================================

def carica_secret_file():
    """
    Carica le variabili dal Secret File di Render, se presente.
    """

    if not os.path.exists(SECRET_FILE):
        print("ℹ️ Secret File non presente.")
        return

    try:

        with open(
            SECRET_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            for riga in file:

                riga = riga.strip()

                if not riga:
                    continue

                if riga.startswith("#"):
                    continue

                if "=" not in riga:
                    continue

                nome, valore = riga.split(
                    "=",
                    1
                )

                nome = nome.strip()
                valore = valore.strip()

                # Rimuove eventuali virgolette
                valore = (
                    valore
                    .strip('"')
                    .strip("'")
                    .strip()
                )

                # Corregge un eventuale "\:"
                # trasformandolo nel normale ":"
                valore = valore.replace(
                    "\\:",
                    ":"
                )

                if nome and valore:
                    os.environ[nome] = valore

        print("✅ Secret File caricato correttamente.")

    except Exception as errore:

        print(
            f"⚠️ Errore lettura Secret File: {errore}"
        )


# ============================================================
# CARICAMENTO SECRET FILE
# ============================================================

carica_secret_file()


# ============================================================
# LETTURA VARIABILI FINALI
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

FOOTBALL_API_KEY = os.getenv(
    "FOOTBALL_API_KEY",
    ""
).strip()


# ============================================================
# PULIZIA TOKEN TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = (
    TELEGRAM_BOT_TOKEN
    .strip()
    .strip('"')
    .strip("'")
    .strip()
)

# Corregge eventuale "\:" residuo
TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN.replace(
    "\\:",
    ":"
)

# Mantiene il formato:
# PARTE_NUMERICA:PARTE_TOKEN

if ":" in TELEGRAM_BOT_TOKEN:

    parte_id, parte_token = (
        TELEGRAM_BOT_TOKEN.split(
            ":",
            1
        )
    )

    TELEGRAM_BOT_TOKEN = (
        parte_id.strip()
        + ":"
        + parte_token.strip()
    )


# ============================================================
# VARIABILI GLOBALI
# ============================================================

bot = None

cache_api = {}
cache_sportsdb = {}

CACHE_API_SECONDS = 300
CACHE_SPORTSDB_SECONDS = 900


# ============================================================
# CAMPIONATI
# ============================================================

CAMPIONATI = {

    "serie a": {
        "nome": "🇮🇹 Serie A",
        "api_id": 135,
        "sportsdb_id": 4332
    },

    "premier league": {
        "nome": "🏴 Premier League",
        "api_id": 39,
        "sportsdb_id": 4328
    },

    "la liga": {
        "nome": "🇪🇸 La Liga",
        "api_id": 140,
        "sportsdb_id": 4335
    },

    "bundesliga": {
        "nome": "🇩🇪 Bundesliga",
        "api_id": 78,
        "sportsdb_id": 4331
    },

    "ligue 1": {
        "nome": "🇫🇷 Ligue 1",
        "api_id": 61,
        "sportsdb_id": 4334
    }
}


# ============================================================
# VERIFICA CONFIGURAZIONE
# ============================================================

print()

print(
    "TELEGRAM_BOT_TOKEN:",
    "OK" if TELEGRAM_BOT_TOKEN else "MANCANTE"
)

print(
    "FOOTBALL_API_KEY:",
    "OK" if FOOTBALL_API_KEY else "MANCANTE"
)

print()


# ============================================================
# CREAZIONE BOT TELEGRAM
# ============================================================

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non configurato. "
        "Inseriscilo nelle Environment Variables / "
        "Secret Files di Render."
    )

print("========== CONTROLLO TOKEN ==========")
print("Token presente:", bool(TELEGRAM_BOT_TOKEN))
print("Lunghezza token:", len(TELEGRAM_BOT_TOKEN))
print("Numero di ':' nel token:", TELEGRAM_BOT_TOKEN.count(":"))
print("Token contiene spazi:", " " in TELEGRAM_BOT_TOKEN)
print("Token contiene '\\\\':", "\\" in TELEGRAM_BOT_TOKEN)
print("Token contiene virgolette:", '"' in TELEGRAM_BOT_TOKEN or "'" in TELEGRAM_BOT_TOKEN)
print("======================================")

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)


# ============================================================
# FUNZIONE HTTP GENERICA
# ============================================================

def richiesta_http(
    url,
    params=None,
    headers=None,
    timeout=API_TIMEOUT
):

    try:

        risposta = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout
        )

        print()
        print("🌐 RICHIESTA HTTP")
        print("URL:", risposta.url)
        print("HTTP:", risposta.status_code)

        if risposta.status_code != 200:

            print(
                "❌ HTTP ERROR:",
                risposta.text[:500]
            )

            return None

        try:

            return risposta.json()

        except Exception:

            print(
                "❌ Risposta non JSON."
            )

            return None

    except requests.exceptions.Timeout:

        print(
            "❌ Timeout HTTP."
        )

        return None

    except requests.exceptions.RequestException as errore:

        print(
            f"❌ Errore HTTP: {errore}"
        )

        return None

    except Exception as errore:

        print(
            f"❌ Errore generico HTTP: {errore}"
        )

        return None


# ============================================================
# API-FOOTBALL
# ============================================================

def api_football_get(
    endpoint,
    params=None
):

    if not FOOTBALL_API_KEY:

        print(
            "⚠️ FOOTBALL_API_KEY non configurata."
        )

        return None

    chiave_cache = (
        endpoint,
        tuple(
            sorted(
                (params or {}).items()
            )
        )
    )

    adesso = time.time()

    if chiave_cache in cache_api:

        timestamp, dati = (
            cache_api[chiave_cache]
        )

        if (
            adesso - timestamp
            < CACHE_API_SECONDS
        ):

            print(
                "♻️ API-Football: utilizzo cache."
            )

            return dati

    url = (
        f"{API_FOOTBALL_BASE}/{endpoint}"
    )

    headers = {
        "x-apisports-key": FOOTBALL_API_KEY
    }

    print()
    print(
        "=========================================="
    )
