import os
import re
import json
import math
import time
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests
import telebot
from telebot import types


# ============================================================
# CONFIGURAZIONE
# ============================================================

PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://bot-pronostici-gratis.onrender.com"
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = RENDER_EXTERNAL_URL + WEBHOOK_PATH

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# ============================================================
# CAMPIONATI
# ============================================================

CAMPIONATI = {
    "serie_a": {
        "nome": "🇮🇹 Serie A",
        "league": "ita.1",
    },
    "premier": {
        "nome": "🏴 Premier League",
        "league": "eng.1",
    },
    "liga": {
        "nome": "🇪🇸 La Liga",
        "league": "esp.1",
    },
    "bundesliga": {
        "nome": "🇩🇪 Bundesliga",
        "league": "ger.1",
    },
    "ligue1": {
        "nome": "🇫🇷 Ligue 1",
        "league": "fra.1",
    },
}


# ============================================================
# CACHE
# ============================================================

CACHE = {}
CACHE_TTL = 30 * 60

CACHE_LOCK = threading.Lock()


def cache_get(key):
    with CACHE_LOCK:
        item = CACHE.get(key)

        if not item:
            return None

        timestamp, value = item

        if time.time() - timestamp > CACHE_TTL:
            del CACHE[key]
            return None

        return value


def cache_set(key, value):
    with CACHE_LOCK:
        CACHE[key] = (time.time(), value)


# ============================================================
# HTTP ESPN
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
})


def espn_get_url(url, timeout=20):
    try:
        response = SESSION.get(url, timeout=timeout)

        if response.status_code != 200:
            print(
                f"ESPN HTTP {response.status_code}: {url}"
            )
            return None

        return response.json()

    except Exception as e:
        print(f"Errore ESPN: {e}")
        return None


def espn_get(path, params=None, timeout=20):
    url = ESPN_BASE + path

    if params:
        query = "&".join(
            f"{key}={value}"
            for key, value in params.items()
        )
        url += "?" + query

    return espn_get_url(url, timeout=timeout)


# ============================================================
# UTILITÀ
# ============================================================

def normalizza_testo(text):
    if not text:
        return ""

    text = str(text)

    text = unicodedata.normalize(
        "NFKD",
        text
    )

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text
    )

    return " ".join(text.split())


def numero(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def clamp(value, minimo, massimo):
    return max(minimo, min(massimo, value))


def media(lista):
    valori = [
        numero(x)
        for x in lista
        if x is not None
    ]

    if not valori:
        return 0.0

    return sum(valori) / len(valori)


def poisson_probability(lam, goals):
    if lam < 0:
        lam = 0

    try:
        return (
            math.exp(-lam)
            * (lam ** goals)
            / math.factorial(goals)
        )
    except Exception:
        return 0.0


def probabilita_1x2(xg_home, xg_away):
    matrix = {}

    for h in range(10):
        for a in range(10):
            p_h = poisson_probability(xg_home, h)
            p_a = poisson_probability(xg_away, a)

            matrix[(h, a)] = p_h * p_a

    home = 0.0
    draw = 0.0
    away = 0.0

    for (h, a), probability in matrix.items():

        if h > a:
            home += probability

        elif h == a:
            draw += probability

        else:
            away += probability

    totale = home + draw + away

    if totale <= 0:
        return {
            "home": 0.3333,
            "draw": 0.3333,
            "away": 0.3334,
        }

    return {
        "home": home / totale,
        "draw": draw / totale,
        "away": away / totale,
    }


# ============================================================
# DATE
