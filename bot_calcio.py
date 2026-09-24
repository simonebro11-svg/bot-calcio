import os
import re
import json
import time
import itertools
import math
import signal
import queue
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import telebot
from telebot import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

# Tipi di update gestiti: da passare SEMPRE esplicitamente a
# set_webhook e infinity_polling. Senza questo Telegram usa
# l'impostazione precedente (sticky): se storicamente erano
# attivi solo i "message", i click sui pulsanti
# (callback_query) NON vengono MAI consegnati.
ALLOWED_UPDATES = ["message", "callback_query"]

# Token segreto per autenticare le richieste webhook di Telegram.
# Se non fornito viene generato al pronto e passato a set_webhook.
TELEGRAM_SECRET_TOKEN = (
    os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()
    or os.urandom(16).hex()
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

HTTP_TIMEOUT = 15

# Cache ESPN
CACHE_TTL = 30 * 60
CACHE_TTL_NEGATIVE = 6 * 60
CACHE_MAX_KEYS = 500

# Giorni futuri da analizzare
GIORNI_FUTURI = 14

# Numero partite nel report
NUM_PARTITE_REPORT = 8

# Numero partite forma recente
NUM_FORM = 10

# Giorni da analizzare per la forma
GIORNI_FORM = 90

# Massimo giorni di scansione nel fallback H2H giornaliero
# (usato solo se l'endpoint team-schedule non risponde)
H2H_FALLBACK_GIORNI = 120

# Fattore sicurezza: le partite devono essere terminate da almeno
# 4 ore per essere considerate completate (evita match in corso).
TERMINAZIONE_MIN_ORE = 4

# Concorrenza analisi massime simultanee
ANALISI_MAX_SIMULTANEE = 2

# Payout per la stima delle quote (94% = margine medio bookmaker):
# quota_stimata = 0.94 * 100 / probabilita
QUOTA_PAYOUT = 0.94

# --------------------------------------------------------
# CALIBRAZIONE MERCATI (backtest su 1000 partite delle 5
# grandi leghe, stagione 2025-26: vedere backtest.py,
# sezione calibrazione_mercati).
#
# Frequenze reali: Over2.5=52.9%, Gol/BTTS=52.6%,
# 1X2=(45/23/31). I mercati goal del modello sono
# SOVRAFFIDATI: lo shrinkage p_cal = w*p + (1-w)*base
# li riporta a probabilita' oneste (log loss: Over
# 0.703->0.688, Gol 0.795->0.692 circa, 1X2 -0.5%).
# --------------------------------------------------------

CAL_BASE_OVER25 = 52.9
CAL_W_OVER25 = 0.35

# Scala completa Over X.5 calibrata su 1000 partite
# (frequenze reali dei totali gol e peso di shrinkage
# per ogni soglia: vedere backtest.py, 'SCALA OVER/UNDER')
CAL_SCALA_BASE = {
    0: 93.7,
    1: 75.4,
    2: 52.9,
    3: 30.2,
    4: 14.9
}

CAL_SCALA_W = {
    0: 0.20,
    1: 0.30,
    2: 0.35,
    3: 0.35,
    4: 0.40
}

# Doppia chance 12 sovraconfidente dal modello
# (backtest 1000 p.): shrinkage forte verso la base
DC12_W = 0.20
DC12_BASE = 76.6

CAL_BASE_GOL = 52.6
CAL_W_GOL = 0.15

CAL_BASE_1X2 = (45.0, 23.0, 31.0)
CAL_W_1X2 = 0.75

MODALITA = "avvio"


# ============================================================
# CONTROLLO CONFIGURAZIONE
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non configurato nelle Environment Variables."
    )

print("TELEGRAM_BOT_TOKEN: OK")
print("⚽ Dati calcistici: ESPN (endpoint team-schedule ottimizzati)")


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)

# Coda aggiornamenti + singolo worker (thread-safe, sequenziale)
_UPDATE_QUEUE: "queue.Queue[bytes]" = queue.Queue()


# ============================================================
# ESPN
# ============================================================

ESPN_BASE = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer"
)


CAMPIONATI = {
    "🇮🇹 Serie A": {
        "espn": "ita.1",
        "nome": "Serie A"
    },
    "🏴 Premier League": {
        "espn": "eng.1",
        "nome": "Premier League"
    },
    "🇪🇸 La Liga": {
        "espn": "esp.1",
        "nome": "La Liga"
    },
    "🇩🇪 Bundesliga": {
        "espn": "ger.1",
        "nome": "Bundesliga"
    },
    "🇫🇷 Ligue 1": {
        "espn": "fra.1",
        "nome": "Ligue 1"
    }
}


COMPETIZIONI_EUROPEE = {
    "uefa.champions": "Champions League",
    "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League"
}

# Slug senza valore statistico (non contano per forma/H2H)
SLUG_INUTILI = {"club.friendly"}

# ============================================================
# SOTTOMENU (nazionali + coppe europee)
#
# Ogni sottomenu ha le sue competizioni e le STESSE 4 schedine
# (SICURA/EQUILIBRATA/AUDACE/MITO) costruite SOLO sulle
# partite delle competizioni del sottomenu (stessa logica
# dell'ottimizzatore: fasce, prob_min, pool a 3 gruppi).
# ============================================================

SOTTOMENU = {
    "naz": {
        "titolo": "🌍 Nazionali",
        "scope": "naz",
        "competizioni": [
            {
                "espn": "uefa.nations",
                "nome": "UEFA Nations League",
                "btn": "🌍 Nations League"
            }
        ]
    },
    "europa": {
        "titolo": "🏆 Champions & Europa",
        "scope": "europa",
        "competizioni": [
            {
                "espn": "uefa.champions",
                "nome": "Champions League",
                "btn": "⭐ Champions League"
            },
            {
                "espn": "uefa.europa",
                "nome": "Europa League",
                "btn": "🥈 Europa League"
            }
        ]
    }
}


def config_competizione(
    key: str
) -> Optional[Dict[str, Any]]:

    """Config di una competizione: campionati club (per
    chiave display) o competizioni dei sottomenu (per
    slug ESPN). None se sconosciuta."""

    if key in CAMPIONATI:
        return CAMPIONATI[key]

    for cat in SOTTOMENU.values():

        for c in cat["competizioni"]:

            if c["espn"] == key:

                return {
                    "espn": c["espn"],
                    "nome": c["nome"]
                }

    return None


def slug_per_scope(
    scope: Optional[str]
) -> Optional[List[str]]:

    """Slug ESPN delle competizioni di un sottomenu
    (None = pool club classico dei 5 campionati)."""

    if not scope:
        return None

    for cat in SOTTOMENU.values():

        if cat["scope"] == scope:

            return [
                c["espn"]
                for c in cat["competizioni"]
            ]

    return None


# ============================================================
# CACHE (LRU con TTL, supporto risultati negativi)
# ============================================================

_CACHE: Dict[str, Tuple[float, Any, int]] = {}

_CACHE_LOCK = threading.Lock()


def cache_get(
    key: str
):
    with _CACHE_LOCK:

        item = _CACHE.get(key)

        if not item:
            return None

        timestamp, value, ttl = item

        if time.time() - timestamp > ttl:
            del _CACHE[key]
            return None

        # porta in coda (LRU)
        _CACHE.pop(key)
        _CACHE[key] = item

        return value


def cache_set(
    key: str,
    value: Any,
    ttl: int = CACHE_TTL
):
    with _CACHE_LOCK:

        if len(_CACHE) >= CACHE_MAX_KEYS:
            for _ in range(CACHE_MAX_KEYS // 4):
                try:
                    vecchio = next(iter(_CACHE))
                    del _CACHE[vecchio]
                except StopIteration:
                    break

        _CACHE[key] = (
            time.time(),
            value,
            ttl
        )


# ============================================================
# UTILITY
# ============================================================

def normalizza_nome(
    nome: str
) -> str:

    if not nome:
        return ""

    testo = unicodedata.normalize(
        "NFKD",
        str(nome)
    )

    testo = "".join(
        c for c in testo
        if not unicodedata.combining(c)
    )

    testo = testo.lower()

    sostituzioni = {
        "internazionale": "inter",
        "internazionale milano": "inter",
        "inter milan": "inter",
        "ac milan": "milan",
        "milan ac": "milan",
        "ss lazio": "lazio",
        "ss lazio roma": "lazio",
        "as roma": "roma",
        "as roma calcio": "roma",
        "juventus fc": "juventus",
        "fc juventus": "juventus",
        "paris saint-germain": "psg",
        "paris saint germain": "psg",
        "psg": "psg",
        "manchester united fc": "manchester united",
        "manchester city fc": "manchester city",
        "tottenham hotspur": "tottenham",
        "tottenham hotspur fc": "tottenham",
    }

    testo = re.sub(
        r"[^a-z0-9\s]",
        " ",
        testo
    )

    testo = re.sub(
        r"\s+",
        " ",
        testo
    ).strip()

    return sostituzioni.get(
        testo,
        testo
    )


def safe_float(
    value,
    default=0.0
) -> float:

    try:

        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(
                ",",
                "."
            )

        return float(value)

    except Exception:
        return default


def safe_int(
    value,
    default=0
) -> int:

    try:
        return int(value)

    except Exception:
        return default


def clamp(
    value: float,
    minimo: float,
    massimo: float
) -> float:

    return max(
        minimo,
        min(massimo, value)
    )


def format_percent(
    value: float
) -> str:

    return f"{clamp(value, 0, 100):.0f}%"


def data_italiana(
    dt: datetime
) -> str:

    return dt.strftime(
        "%d/%m/%Y"
    )


def html_safe(
    testo: str
) -> str:

    return (
        str(testo or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def parse_datetime(
    value: str
) -> Optional[datetime]:

    if not value:
        return None

    try:

        testo = value.replace(
            "Z",
            "+00:00"
        )

        dt = datetime.fromisoformat(
            testo
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:
        return None


def estrai_competitors(
    evento: Dict[str, Any]
) -> Tuple[
    Optional[Dict],
    Optional[Dict]
]:

    competitions = evento.get(
        "competitions",
        []
    )

    if not competitions:
        return None, None

    competitors = competitions[0].get(
        "competitors",
        []
    )

    home = None
    away = None

    for competitor in competitors:

        if competitor.get(
            "homeAway"
        ) == "home":

            home = competitor

        elif competitor.get(
            "homeAway"
        ) == "away":

            away = competitor

    return home, away


def estrai_score(
    competitor: Dict[str, Any]
) -> Optional[int]:

    score = competitor.get(
        "score"
    )

    if score is None:
        return None

    if isinstance(score, dict):

        score = (
            score.get("displayValue")
            or score.get("value")
            or score.get("alternateDisplayValue")
        )

    if score is None:
        return None

    try:
        return int(float(score))

    except Exception:
        return None


def estrai_nome_team(
    competitor: Dict[str, Any]
) -> str:

    team = competitor.get(
        "team",
        {}
    )

    return (
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or "Sconosciuta"
    )


def estrai_id_team(
    competitor: Dict[str, Any]
) -> Optional[str]:

    team = competitor.get(
        "team",
        {}
    )

    value = team.get("id")

    if value is None:
        return None

    return str(value)


def slug_competizione(
    evento: Dict[str, Any]
) -> str:

    league_obj = evento.get(
        "league",
        {}
    )

    if not isinstance(league_obj, dict):
        return ""

    return str(
        league_obj.get("slug") or ""
    )


def evento_terminato(
    evento: Dict[str, Any],
    oggi: Optional[datetime] = None
) -> bool:

    if oggi is None:
        oggi = datetime.now(timezone.utc)

    # 1) Eventi scoreboard: campo status esplicito
    status = (
        evento.get("status", {})
        .get("type", {})
    )

    if isinstance(status, dict) and status:
        return bool(
            status.get("completed")
            or status.get("state") == "post"
            or status.get("name") in {
                "STATUS_FINAL",
                "STATUS_FULL_TIME"
            }
        )

    # 2) Eventi team-schedule: nessun campo status ->
    #    score presenti + data nel passato da almeno
    #    TERMINAZIONE_MIN_ORE ore (evita match in corso)
    dt = parse_datetime(
        evento.get("date", "")
    )

    if not dt:
        return False

    if dt >= oggi:
        return False

    if (
        oggi - dt
    ) < timedelta(
        hours=TERMINAZIONE_MIN_ORE
    ):
        return False

    home, away = (
        estrai_competitors(
            evento
        )
    )

    if not home or not away:
        return False

    return (
        estrai_score(home) is not None
        and estrai_score(away) is not None
    )


def anni_stagione(
    oggi: Optional[datetime] = None
) -> List[int]:

    """Anni 'ESPN' delle ultime 3 stagioni.

    L'anno stagione ESPN è l'anno d'inizio del campionato
    (es. 2026-27 -> 2026). Tre stagioni: corrente + 2
    precedenti (aumenta la copertura H2H, costo: 3
    richieste per squadra, sempre memorizzate in cache).
    """

    if oggi is None:
        oggi = datetime.now(timezone.utc)

    corrente = (
        oggi.year
        if oggi.month >= 7
        else oggi.year - 1
    )

    return [
        corrente,
        corrente - 1,
        corrente - 2
    ]


# ============================================================
# HTTP ESPN (con retry)
# ============================================================

def espn_get_url(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    cache_key: Optional[str] = None,
    use_cache: bool = True
):

    if cache_key and use_cache:

        cached = cache_get(
            cache_key
        )

        if cached is not None:
            return cached

    for attempt in (1, 2):

        try:

            response = requests.get(
                url,
                params=params or {},
                timeout=HTTP_TIMEOUT
            )

            if response.status_code == 200:

                data = response.json()

                if cache_key and use_cache:
                    cache_set(
                        cache_key,
                        data
                    )

                return data

            if (
                response.status_code in {
                    429, 500, 502, 503, 504
                }
                and attempt == 1
            ):
                time.sleep(1.0)
                continue

            print(
                f"⚠️ ESPN HTTP "
                f"{response.status_code}: "
                f"{url}"
            )

            return None

        except requests.RequestException as exc:

            if attempt == 1:
                time.sleep(1.0)
                continue

            print(
                f"❌ Errore richiesta ESPN: "
                f"{exc}"
            )

            return None

        except ValueError as exc:

            print(
                f"❌ JSON ESPN non valido: "
                f"{exc}"
            )

            return None

        except Exception as exc:

            print(
                f"❌ Errore ESPN generico: "
                f"{exc}"
            )

            return None

    return None


def espn_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    cache_key: Optional[str] = None,
    use_cache: bool = True
):

    url = (
        f"{ESPN_BASE}/"
        f"{path.lstrip('/')}"
    )

    return espn_get_url(
        url,
        params=params,
        cache_key=cache_key,
        use_cache=use_cache
    )


# ============================================================
# STORIA SQUADRA (endpoint ottimizzato)
#
# all/teams/{id}/schedule?season=YYYY restituisce TUTTE le
# competizioni della stagione (campionato, coppe, UEFA,
# amichevole) con 1 sola chiamata. Richiedendo stagione
# corrente e precedente copriamo:
#   • forma recente (90 giorni)
#   • H2H (tutte le competizioni)
#   • impegni europei
#   • riposo / fatica
# ============================================================

def _storia_stagione(
    team_id: str,
    season: int
) -> List[Dict[str, Any]]:

    cache_key = (
        f"storia_raw_{team_id}_"
        f"{season}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    data = espn_get(
        f"all/teams/{team_id}/schedule",
        params={
            "season": season
        },
        cache_key=cache_key
    )

    if not data:
        return []

    eventi = data.get(
        "events"
    )

    if not isinstance(eventi, list):
        return []

    return eventi


def storia_squadra(
    team_id: Optional[str],
    oggi: Optional[datetime] = None
) -> Optional[List[Dict[str, Any]]]:

    """Storia completa (2 stagioni, tutte competizioni)
    di una squadra, ordinata dalla più recente.

    None = team id assente o endpoint non disponibile.
    """

    if not team_id:
        return None

    if oggi is None:
        oggi = datetime.now(timezone.utc)

    cache_key = (
        f"storia_team_{team_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    print(
        f"📜 Storia squadra: "
        f"team {team_id} "
        f"(stagioni {anni_stagione(oggi)})"
    )

    visti = set()
    eventi = []

    for season in anni_stagione(
        oggi
    ):

        for evento in (
            _storia_stagione(
                team_id,
                season
            )
        ):

            event_id = str(
                evento.get(
                    "id",
                    ""
                )
            ).strip()

            if not event_id:
                continue

            if event_id in visti:
                continue

            visti.add(event_id)
            eventi.append(evento)

    def _chiave_data(evento):

        dt = parse_datetime(
            evento.get(
                "date",
                ""
            )
        )

        return dt or datetime.min.replace(
            tzinfo=timezone.utc
        )

    eventi.sort(
        key=_chiave_data,
        reverse=True
    )

    print(
        f"   📜 {len(eventi)} eventi "
        f"totali per team {team_id}"
    )

    cache_set(
        cache_key,
        eventi
    )

    return eventi


# ============================================================
# PARTITE FUTURE
# ============================================================

def recupera_partite_future(
    campionato: str
) -> List[Dict[str, Any]]:

    print(
        f"\U0001f4c5 Recupero partite future: "
        f"{campionato}"
    )

    cache_key = (
        f"future_matches_{campionato}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:

        print(
            f"\U0001f4e6 Uso cache: "
            f"{len(cached)} partite"
        )

        return cached

    oggi = datetime.now(
        timezone.utc
    )

    def _fetch(data_str: str):

        return espn_get(
            f"{campionato}/scoreboard",
            params={
                "dates": data_str
            },
            cache_key=(
                f"scoreboard_"
                f"{campionato}_"
                f"{data_str}"
            )
        )

    def _scandici(num_giorni: int):

        """Scansiona i prossimi num_giorni giorni
        (richieste parallele, cache per giorno) e
        ritorna gli eventi futuri ordinati per data."""

        date = [
            (
                oggi
                + timedelta(days=i)
            ).strftime("%Y%m%d")
            for i in range(num_giorni)
        ]

        with ThreadPoolExecutor(
            max_workers=6
        ) as executor:

            risposte = list(
                executor.map(
                    _fetch,
                    date
                )
            )

        eventi = []
        ids_visti = set()

        for data in risposte:

            if not data:
                continue

            for evento in (
                data.get("events", [])
            ):

                event_id = str(
                    evento.get(
                        "id",
                        ""
                    )
                ).strip()

                if not event_id:
                    continue

                if event_id in ids_visti:
                    continue

                dt = parse_datetime(
                    evento.get(
                        "date",
                        ""
                    )
                )

                if not dt:
                    continue

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)

                if dt <= oggi:
                    continue

                home, away = (
                    estrai_competitors(
                        evento
                    )
                )

                if not home or not away:
                    continue

                ids_visti.add(event_id)

                evento["_datetime"] = dt

                eventi.append(evento)

        eventi.sort(
            key=lambda x:
            x.get("_datetime")
            or datetime.max.replace(
                tzinfo=timezone.utc
            )
        )

        return eventi

    # Finestra adattiva: durante le pause di calendario
    # (raduni nazionali, esono pernottamenti) nei primi
    # 14 giorni puo' non esserci nulla: si allarga la
    # ricerca fino a 42 giorni con arresto anticipato.
    eventi = []
    finestra_usata = GIORNI_FUTURI

    for finestra in dict.fromkeys(
        [
            GIORNI_FUTURI,
            GIORNI_FUTURI * 2,
            GIORNI_FUTURI * 3
        ]
    ):

        print(
            f"\U0001f50e Ricerca ESPN: prossimi "
            f"{finestra} giorni "
            f"({finestra} richieste, cache per giorno)"
        )

        eventi = _scandici(finestra)

        finestra_usata = finestra

        if len(eventi) >= NUM_PARTITE_REPORT:
            break

        if eventi and finestra == max(
            GIORNI_FUTURI * 3,
            GIORNI_FUTURI
        ):
            break

    risultati = eventi[
        :NUM_PARTITE_REPORT
    ]

    cache_set(
        cache_key,
        risultati
    )

    print(
        f"\U0001f4c5 Partite future trovate: "
        f"{len(risultati)} "
        f"(finestra {finestra_usata} gg)"
    )

    return risultati


# ============================================================
# FORM RECENTE
# ============================================================

def _match_da_evento(
    evento: Dict[str, Any],
    team_name: str
) -> Optional[Dict[str, Any]]:

    home, away = (
        estrai_competitors(
            evento
        )
    )

    if not home or not away:
        return None

    score_home = estrai_score(
        home
    )

    score_away = estrai_score(
        away
    )

    if (
        score_home is None
        or score_away is None
    ):
        return None

    dt = parse_datetime(
        evento.get(
            "date",
            ""
        )
    )

    if not dt:
        return None

    return {
        "id": str(
            evento.get("id", "")
        ),
        "date": dt,
        "home": estrai_nome_team(
            home
        ),
        "away": estrai_nome_team(
            away
        ),
        "home_score": score_home,
        "away_score": score_away,
        "team": team_name,
        "league": slug_competizione(
            evento
        )
    }


def _form_da_scoreboard(
    team_name: str,
    league: str
) -> List[Dict[str, Any]]:

    """Fallback lento: scandisce lo scoreboard
    giornaliero (con stop anticipato quando si
    raggiungono NUM_FORM partite).
    """

    oggi = datetime.now(
        timezone.utc
    )

    nome_norm = normalizza_nome(
        team_name
    )

    partite = []
    ids_visti = set()

    for offset in range(
        GIORNI_FORM
    ):

        giorno = (
            oggi
            - timedelta(days=offset)
        )

        data_str = giorno.strftime(
            "%Y%m%d"
        )

        data = espn_get(
            f"{league}/scoreboard",
            params={
                "dates": data_str
            },
            cache_key=(
                f"form_score_"
                f"{league}_"
                f"{data_str}"
            )
        )

        if not data:
            continue

        for evento in data.get(
            "events",
            []
        ):

            if not evento_terminato(
                evento,
                oggi
            ):
                continue

            event_id = str(
                evento.get(
                    "id",
                    ""
                )
            )

            if (
                not event_id
                or event_id in ids_visti
            ):
                continue

            home, away = (
                estrai_competitors(
                    evento
                )
            )

            if not home or not away:
                continue

            nome_home = estrai_nome_team(
                home
            )

            nome_away = estrai_nome_team(
                away
            )

            if (
                normalizza_nome(
                    nome_home
                ) != nome_norm
                and
                normalizza_nome(
                    nome_away
                ) != nome_norm
            ):
                continue

            match = _match_da_evento(
                evento,
                team_name
            )

            if not match:
                continue

            ids_visti.add(
                event_id
            )

            partite.append(
                match
            )

        if len(partite) >= NUM_FORM:
            break

    return partite


def recupera_form_da_eventi(
    team_name: str,
    league: str,
    team_id: Optional[str] = None
) -> List[Dict[str, Any]]:

    cache_key = (
        f"form_{league}_"
        f"{normalizza_nome(team_name)}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    oggi = datetime.now(
        timezone.utc
    )

    limite = oggi - timedelta(
        days=GIORNI_FORM
    )

    partite: List[Dict[str, Any]] = []

    storia = (
        storia_squadra(
            team_id,
            oggi
        )
        if team_id
        else None
    )

    # Storia non disponibile (endpoint guasto o id
    # assente) -> fallback lento sullo scoreboard
    if not storia:
        storia = None

    if storia is not None:

        for evento in storia:

            if slug_competizione(
                evento
            ) in SLUG_INUTILI:
                continue

            dt = parse_datetime(
                evento.get(
                    "date",
                    ""
                )
            )

            if not dt or dt < limite:
                continue

            if not evento_terminato(
                evento,
                oggi
            ):
                continue

            match = _match_da_evento(
                evento,
                team_name
            )

            if match:
                partite.append(match)

    else:

        partite = _form_da_scoreboard(
            team_name,
            league
        )

    partite.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    partite = partite[
        :NUM_FORM
    ]

    cache_set(
        cache_key,
        partite
    )

    return partite


def risultato_team(
    match: Dict[str, Any],
    team_name: str
) -> str:

    nome_norm = normalizza_nome(
        team_name
    )

    home_norm = normalizza_nome(
        match["home"]
    )

    away_norm = normalizza_nome(
        match["away"]
    )

    hg = match["home_score"]
    ag = match["away_score"]

    if home_norm == nome_norm:

        if hg > ag:
            return "V"

        elif hg < ag:
            return "S"

        return "P"

    if away_norm == nome_norm:

        if ag > hg:
            return "V"

        elif ag < hg:
            return "S"

        return "P"

    return "?"


def statistiche_form(
    form: List[Dict[str, Any]],
    team_name: str
) -> Dict[str, Any]:

    risultati = []

    punti = 0
    gol_fatti = 0
    gol_subiti = 0

    team_norm = normalizza_nome(
        team_name
    )

    for match in form:

        risultato = risultato_team(
            match,
            team_name
        )

        if risultato == "?":
            continue

        home = normalizza_nome(
            match["home"]
        )

        risultati.append(
            risultato
        )

        if home == team_norm:

            gf = match["home_score"]
            gs = match["away_score"]

        else:

            gf = match["away_score"]
            gs = match["home_score"]

        gol_fatti += gf
        gol_subiti += gs

        if risultato == "V":
            punti += 3

        elif risultato == "P":
            punti += 1

    numero = len(form)

    ppg = (
        punti / numero
        if numero
        else 0
    )

    gf_media = (
        gol_fatti / numero
        if numero
        else 0
    )

    gs_media = (
        gol_subiti / numero
        if numero
        else 0
    )

    return {
        "sequenza": (
            "".join(risultati)
            or "N/D"
        ),
        "ppg": ppg,
        "gf": gf_media,
        "gs": gs_media,
        "gol_fatti": gol_fatti,
        "gol_subiti": gol_subiti,
        "partite": numero
    }


def rendimento_casa(
    form: List[Dict[str, Any]],
    team_name: str
) -> Optional[float]:

    valori = []

    team_norm = normalizza_nome(
        team_name
    )

    for match in form:

        if (
            normalizza_nome(
                match["home"]
            )
            != team_norm
        ):
            continue

        hg = match["home_score"]
        ag = match["away_score"]

        if hg > ag:
            punti = 3

        elif hg == ag:
            punti = 1

        else:
            punti = 0

        valori.append(
            punti
        )

    if not valori:
        return None

    return sum(valori) / len(
        valori
    )


def rendimento_trasferta(
    form: List[Dict[str, Any]],
    team_name: str
) -> Optional[float]:

    valori = []

    team_norm = normalizza_nome(
        team_name
    )

    for match in form:

        if (
            normalizza_nome(
                match["away"]
            )
            != team_norm
        ):
            continue

        hg = match["home_score"]
        ag = match["away_score"]

        if ag > hg:
            punti = 3

        elif ag == hg:
            punti = 1

        else:
            punti = 0

        valori.append(
            punti
        )

    if not valori:
        return None

    return sum(valori) / len(
        valori
    )


def ppg_campionato_squadra(
    team_id: Optional[str],
    team_name: str,
    league: str,
    oggi: Optional[datetime] = None
) -> Optional[float]:

    """PPG di SOLI campionato, ultime 8 partite in lega
    (senza finestra temporale: nelle prime giornate copre
    anche la stagione precedente via storia 3 stagioni).

    Feature calibrata col backtest (peso 3 nel modello).
    Richiede almeno 4 partite, altrimenti None.
    """

    if not team_id or not league:
        return None

    if oggi is None:
        oggi = datetime.now(timezone.utc)

    cache_key = (
        f"ppg_camp_"
        f"{league}_"
        f"{team_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return (
            cached
            if cached != -1
            else None
        )

    storia = (
        storia_squadra(
            team_id,
            oggi
        )
    )

    if not storia:
        cache_set(
            cache_key,
            -1
        )

        return None

    partite = []

    for evento in storia:

        if slug_competizione(
            evento
        ) != league:
            continue

        if not evento_terminato(
            evento,
            oggi
        ):
            continue

        match = _match_da_evento(
            evento,
            team_name
        )

        if match:
            partite.append(
                match
            )

        if len(partite) >= 8:
            break

    if len(partite) < 4:

        cache_set(
            cache_key,
            -1
        )

        return None

    punti = 0

    for match in partite:

        risultato = risultato_team(
            match,
            team_name
        )

        if risultato == "V":
            punti += 3

        elif risultato == "P":
            punti += 1

    ppg = punti / len(
        partite
    )

    cache_set(
        cache_key,
        ppg
    )

    return ppg


# ============================================================
# H2H
# ============================================================

def _h2h_da_storia(
    storia: Optional[List[Dict[str, Any]]],
    home_name: str,
    away_name: str,
    home_team_id: Optional[str],
    away_team_id: Optional[str],
    oggi: datetime
) -> List[Dict[str, Any]]:

    if not storia:
        return []

    home_norm = normalizza_nome(
        home_name
    )

    away_norm = normalizza_nome(
        away_name
    )

    risultati = []

    for evento in storia:

        if slug_competizione(
            evento
        ) in SLUG_INUTILI:
            continue

        if not evento_terminato(
            evento,
            oggi
        ):
            continue

        home, away = (
            estrai_competitors(
                evento
            )
        )

        if not home or not away:
            continue

        if home_team_id and away_team_id:

            id_home = estrai_id_team(
                home
            )

            id_away = estrai_id_team(
                away
            )

            if (
                id_home and id_away
                and {
                    id_home,
                    id_away
                }
                != {
                    home_team_id,
                    away_team_id
                }
            ):
                continue

        else:

            eh = estrai_nome_team(
                home
            )

            ea = estrai_nome_team(
                away
            )

            if (
                normalizza_nome(eh) != home_norm
                and normalizza_nome(eh) != away_norm
            ) or (
                normalizza_nome(ea) != home_norm
                and normalizza_nome(ea) != away_norm
            ):
                continue

        hs = estrai_score(
            home
        )

        ascore = estrai_score(
            away
        )

        if (
            hs is None
            or ascore is None
        ):
            continue

        dt = parse_datetime(
            evento.get(
                "date",
                ""
            )
        )

        if not dt:
            continue

        risultati.append({
            "date": dt,
            "home": estrai_nome_team(
                home
            ),
            "away": estrai_nome_team(
                away
            ),
            "home_score": hs,
            "away_score": ascore
        })

    return risultati


def recupera_h2h(
    home_name: str,
    away_name: str,
    home_team_id: Optional[str] = None,
    away_team_id: Optional[str] = None,
    league: Optional[str] = None
) -> List[Dict[str, Any]]:

    cache_key = (
        f"h2h_"
        f"{normalizza_nome(home_name)}_"
        f"{normalizza_nome(away_name)}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    oggi = datetime.now(
        timezone.utc
    )

    storia_home = (
        storia_squadra(
            home_team_id,
            oggi
        )
        if home_team_id
        else None
    )

    storia_away = (
        storia_squadra(
            away_team_id,
            oggi
        )
        if away_team_id
        else None
    )

    risultati = (
        _h2h_da_storia(
            storia_home,
            home_name,
            away_name,
            home_team_id,
            away_team_id,
            oggi
        )
    )

    if not risultati and away_team_id:

        risultati = (
            _h2h_da_storia(
                storia_away,
                home_name,
                away_name,
                home_team_id,
                away_team_id,
                oggi
            )
        )

    # FALLBACK lento: scoreboard giornaliero.
    # Necessario solo se la storia di una delle due
    # squadre NON è disponibile: se entrambe le storie
    # (2 stagioni, tutte le competizioni) sono state
    # recuperate, un confronto inesistente in quel
    # arco non può esistere nemmeno nello scoreboard.
    storia_ok = bool(
        storia_home
    ) and bool(
        storia_away
    )

    if not risultati and league and not storia_ok:

        home_norm = normalizza_nome(
            home_name
        )

        away_norm = normalizza_nome(
            away_name
        )

        print(
            f"🔎 H2H fallback scoreboard: "
            f"{home_name} vs {away_name} "
            f"(max {H2H_FALLBACK_GIORNI} giorni)"
        )

        for offset in range(
            H2H_FALLBACK_GIORNI
        ):

            giorno = (
                oggi
                - timedelta(days=offset)
            )

            data_str = giorno.strftime(
                "%Y%m%d"
            )

            data = espn_get(
                f"{league}/scoreboard",
                params={
                    "dates": data_str
                },
                cache_key=(
                    f"h2h_score_"
                    f"{league}_"
                    f"{data_str}"
                )
            )

            if not data:
                continue

            for evento in data.get(
                "events",
                []
            ):

                if not evento_terminato(
                    evento,
                    oggi
                ):
                    continue

                eh_comp, ea_comp = (
                    estrai_competitors(
                        evento
                    )
                )

                if (
                    not eh_comp
                    or not ea_comp
                ):
                    continue

                eh = estrai_nome_team(
                    eh_comp
                )

                ea = estrai_nome_team(
                    ea_comp
                )

                if (
                    normalizza_nome(eh) != home_norm
                    and normalizza_nome(eh) != away_norm
                ) or (
                    normalizza_nome(ea) != home_norm
                    and normalizza_nome(ea) != away_norm
                ):
                    continue

                hs = estrai_score(
                    eh_comp
                )

                ass = estrai_score(
                    ea_comp
                )

                if (
                    hs is None
                    or ass is None
                ):
                    continue

                dt = parse_datetime(
                    evento.get(
                        "date",
                        ""
                    )
                )

                if not dt:
                    continue

                risultati.append({
                    "date": dt,
                    "home": eh,
                    "away": ea,
                    "home_score": hs,
                    "away_score": ass
                })

            if len(risultati) >= 5:
                break

    risultati.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    risultati = risultati[:5]

    cache_set(
        cache_key,
        risultati
    )

    return risultati


# ============================================================
# INFORTUNI
#
# 1) football-data.org (REALE): se configurata la env var
#    FOOTBALL_DATA_API_KEY (registrazione gratuita su
#    football-data.org), gli infortuni/squalificati vengono
#    presi dal match corrispondente su football-data.org.
#    Free tier: 10 richieste/minuto -> limiter integrato.
# 2) ESPN (fallback): verificato empiricamente che l'endpoint
#    ESPN per gli infortuni calcistici restituisce sempre un
#    oggetto vuoto; si prova comunque e i risultati vuoti
#    vengono memoizzati (TTL 6 h) per non sprecare richieste.
# ============================================================

FD_BASE = "https://api.football-data.org/v4"

FOOTBALL_DATA_API_KEY = os.getenv(
    "FOOTBALL_DATA_API_KEY",
    ""
).strip()

# codici competizione football-data.org
FD_CODICI_LEGA = {
    "ita.1": "SA",
    "eng.1": "PL",
    "esp.1": "PD",
    "ger.1": "BL1",
    "fra.1": "FL1"
}

FD_TIPI = {
    "INJURY": "Infortunato",
    "SUSPENSION": "Squalificato"
}

# limiter: free tier = 10 req/min -> 1 chiamata ogni 6.5s
_FD_LOCK = threading.Lock()
_FD_ULTIMA = [0.0]
FD_MIN_INTERVALLO = 6.5

_FD_LEGHE_VUOTE: Dict[str, float] = {}

# chiave non valida / non autorizzata: salta tutto il FD per 6h
_FD_AUTH_KO: List[float] = [0.0]


def _fd_auth_ko() -> bool:

    return (
        _FD_AUTH_KO[0]
        and time.time() - _FD_AUTH_KO[0]
        < CACHE_TTL_NEGATIVE
    )


def _fd_throttle():

    while True:

        with _FD_LOCK:

            now = time.time()

            attesa = (
                _FD_ULTIMA[0]
                + FD_MIN_INTERVALLO
                - now
            )

            if attesa <= 0:
                _FD_ULTIMA[0] = now
                return

        time.sleep(
            min(attesa, 10)
        )


def _fd_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    cache_key: Optional[str] = None,
    ttl: int = CACHE_TTL
):

    if not FOOTBALL_DATA_API_KEY:
        return None

    if cache_key:

        cached = cache_get(cache_key)

        if cached is not None:
            return (
                cached
                if cached != -1
                else None
            )

    _fd_throttle()

    try:

        response = requests.get(
            f"{FD_BASE}{path}",
            params=params or {},
            headers={
                "X-Auth-Token": (
                    FOOTBALL_DATA_API_KEY
                )
            },
            timeout=HTTP_TIMEOUT
        )

        if response.status_code == 200:

            data = response.json()

            if cache_key:
                cache_set(cache_key, data, ttl)

            return data

        print(
            f"⚠️ football-data HTTP "
            f"{response.status_code}: {path}"
        )

        if response.status_code in {400, 401, 403}:
            _FD_AUTH_KO[0] = time.time()

        if cache_key:
            cache_set(cache_key, -1, CACHE_TTL_NEGATIVE)

        return None

    except Exception as exc:

        print(
            f"❌ Errore football-data: {exc}"
        )

        if cache_key:
            cache_set(cache_key, -1, CACHE_TTL_NEGATIVE)

        return None


def _fd_match_corrispondente(
    evento_fd: Dict[str, Any],
    home_norm: str,
    away_norm: str
) -> bool:

    ht = (
        evento_fd.get("homeTeam", {})
        or {}
    )

    at = (
        evento_fd.get("awayTeam", {})
        or {}
    )

    fh = normalizza_nome(
        ht.get("name") or ht.get("shortName") or ""
    )

    fa = normalizza_nome(
        at.get("name") or at.get("shortName") or ""
    )

    if not fh or not fa:
        return False

    def uguale(a, b):

        return (
            a == b
            or a in b
            or b in a
        )

    return (
        uguale(fh, home_norm)
        and uguale(fa, away_norm)
    ) or (
        uguale(fh, away_norm)
        and uguale(fa, home_norm)
    )


def recupera_infortuni_fd(
    data_partita: Optional[datetime],
    home_name: str,
    away_name: str,
    league: str
) -> Tuple[
    Optional[List[Dict[str, Any]]],
    Optional[List[Dict[str, Any]]]
]:

    """Infortuni reali (football-data.org) per la partita.

    Ritorna (infortuni_home, infortuni_away):
    - liste (anche vuote) = dati ottenuti
    - (None, None) = dati non disponibili
    """

    if (
        not FOOTBALL_DATA_API_KEY
        or not data_partita
        or league not in FD_CODICI_LEGA
    ):
        return None, None

    if _fd_auth_ko():
        return None, None

    # lega segnata 'vuota' di recente -> salto
    probe = _FD_LEGHE_VUOTE.get(league)

    if (
        probe
        and time.time() - probe < CACHE_TTL_NEGATIVE
    ):
        return None, None

    codice = FD_CODICI_LEGA[league]

    dal = (
        data_partita - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    al = (
        data_partita + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    elenco = _fd_get(
        f"/competitions/{codice}/matches",
        params={
            "dateFrom": dal,
            "dateTo": al
        },
        cache_key=(
            f"fd_matches_{codice}_{dal}_{al}"
        )
    )

    partite = (
        elenco.get("matches", [])
        if isinstance(elenco, dict)
        else []
    )

    home_norm = normalizza_nome(home_name)
    away_norm = normalizza_nome(away_name)

    fd_match = None

    for m in partite:

        if _fd_match_corrispondente(
            m,
            home_norm,
            away_norm
        ):

            fd_match = m

            break

    if not fd_match:

        return None, None

    match_id = fd_match.get("id")

    if not match_id:
        return None, None

    dati = _fd_get(
        f"/matches/{match_id}/injuries",
        cache_key=f"fd_inj_{match_id}",
        ttl=12 * 3600
    )

    if dati is None:

        # endpoint non disponibile per questa lega:
        # memoizza per non riprovare a ogni partita
        _FD_LEGHE_VUOTE[league] = time.time()

        return None, None

    lista = dati.get(
        "injuries",
        []
    )

    if not isinstance(lista, list):
        lista = []

    def scheda(lato: str):

        nome_norm = (
            home_norm
            if lato == "h"
            else away_norm
        )

        out = []
        visti = set()

        for item in lista:

            player = (
                item.get("player", {})
                or {}
            )

            team = (
                item.get("team", {})
                or {}
            )

            tn = normalizza_nome(
                team.get("name")
                or team.get("shortName")
                or ""
            )

            if (
                not tn
                or (
                    tn != nome_norm
                    and tn not in nome_norm
                    and nome_norm not in tn
                )
            ):
                continue

            nome = (
                player.get("name")
                or player.get("displayName")
                or player.get("shortName")
                or "Giocatore"
            )

            tipo_raw = str(
                item.get("type")
                or "INJURY"
            ).upper()

            tipo = FD_TIPI.get(
                tipo_raw,
                tipo_raw.capitalize()
            )

            motivo = (
                item.get("reason")
                or item.get("detail")
                or ""
            )

            chiave = (
                normalizza_nome(nome),
                tipo_raw
            )

            if chiave in visti:
                continue

            visti.add(chiave)

            out.append({
                "nome": nome,
                "stato": tipo,
                "motivo": motivo
            })

        return out

    return scheda("h"), scheda("a")


_PROBE_INFORTUNI: Dict[str, Tuple[str, float]] = {}


def estrai_infortuni_ricorsivo(
    obj: Any,
    risultati: Optional[
        List[Dict[str, Any]]
    ] = None
):

    if risultati is None:
        risultati = []

    if isinstance(obj, dict):

        if (
            "athlete" in obj
            and isinstance(
                obj["athlete"],
                dict
            )
        ):
            risultati.append(
                obj
            )

        for value in obj.values():

            estrai_infortuni_ricorsivo(
                value,
                risultati
            )

    elif isinstance(obj, list):

        for item in obj:

            estrai_infortuni_ricorsivo(
                item,
                risultati
            )

    return risultati


def recupera_infortuni(
    team_id: Optional[str],
    league: str
) -> Optional[
    List[Dict[str, Any]]
]:

    if not team_id:
        return None

    # La lega risulta 'vuota' di recente: salto
    probe = _PROBE_INFORTUNI.get(
        league
    )

    if (
        probe
        and probe[0] == "vuoto"
        and time.time() - probe[1] < CACHE_TTL_NEGATIVE
    ):
        return None

    cache_key = (
        f"injuries_{league}_"
        f"{team_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    url = (
        f"{ESPN_BASE}/"
        f"{league}/teams/"
        f"{team_id}/injuries"
    )

    try:

        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT
        )

        if response.status_code != 200:

            print(
                f"⚠️ Infortuni non disponibili "
                f"per team {team_id}: "
                f"HTTP {response.status_code}"
            )

            _PROBE_INFORTUNI[league] = (
                "vuoto",
                time.time()
            )

            return None

        data = response.json()

        lista = (
            estrai_infortuni_ricorsivo(
                data
            )
        )

        if not lista:

            # Endpoint vuoto: memo + cache negativa
            _PROBE_INFORTUNI[league] = (
                "vuoto",
                time.time()
            )

            cache_set(
                cache_key,
                [],
                CACHE_TTL_NEGATIVE
            )

            return None

        _PROBE_INFORTUNI[league] = (
            "ok",
            time.time()
        )

        unici = []
        chiavi = set()

        for item in lista:

            athlete = item.get(
                "athlete",
                {}
            )

            nome = (
                athlete.get(
                    "displayName"
                )
                or athlete.get(
                    "fullName"
                )
                or athlete.get(
                    "shortName"
                )
                or "Giocatore"
            )

            status = item.get(
                "status",
                {}
            )

            stato = (
                status.get("name")
                or status.get("type")
                or item.get("type")
                or "N/D"
            )

            motivo = (
                item.get("details")
                or item.get("description")
                or item.get("reason")
                or ""
            )

            chiave = (
                normalizza_nome(nome),
                str(stato).lower()
            )

            if chiave in chiavi:
                continue

            chiavi.add(
                chiave
            )

            unici.append({
                "nome": nome,
                "stato": stato,
                "motivo": motivo
            })

        cache_set(
            cache_key,
            unici
        )

        return unici

    except Exception as exc:

        print(
            f"❌ Errore recupero "
            f"infortuni: {exc}"
        )

        return None


def format_infortuni(
    infortuni: Optional[
        List[Dict[str, Any]]
    ]
) -> str:

    if infortuni is None:
        return "⚠️ dati non disponibili"

    if not infortuni:
        return "✅ nessuna segnalazione"

    righe = []

    for item in infortuni[:5]:

        nome = item.get(
            "nome",
            "Giocatore"
        )

        stato = item.get(
            "stato",
            "N/D"
        )

        motivo = item.get(
            "motivo",
            ""
        )

        testo = (
            f"• {html_safe(nome)} — "
            f"{html_safe(stato)}"
        )

        if motivo:
            testo += (
                f" ({html_safe(motivo)})"
            )

        righe.append(
            testo
        )

    if len(infortuni) > 5:

        righe.append(
            f"• ... +"
            f"{len(infortuni) - 5}"
        )

    return "\n".join(
        righe
    )


# ============================================================
# METEO (open-meteo.com: gratuito, senza chiave)
#
# Informazione AGGIUNTIVA di lettura: NON modifica le
# probabilita' (nessuna evidenza misurata sui nostri dati).
# Citta' dello stadio da ESPN -> geocoding -> previsioni
# del giorno della partita (disponibili fino a 16 giorni).
# ============================================================

OPEN_METEO_FORECAST = (
    "https://api.open-meteo.com/"
    "v1/forecast"
)

OPEN_METEO_GEO = (
    "https://geocoding-api."
    "open-meteo.com/v1/search"
)

METEO_GG_MAX = 16


def _geocoda_citta(
    citta: str
) -> Optional[Tuple[float, float]]:

    if not citta:
        return None

    cache_key = f"geo_{normalizza_nome(citta)}"

    cached = cache_get(cache_key)

    if cached is not None:
        return (
            tuple(cached)
            if cached != -1
            else None
        )

    try:

        r = requests.get(
            OPEN_METEO_GEO,
            params={
                "name": citta,
                "count": 1,
                "language": "it",
                "format": "json"
            },
            timeout=8
        )

        risultati = (
            r.json().get("results", [])
            if r.status_code == 200
            else []
        )

        if risultati:

            punto = (
                float(risultati[0]["latitude"]),
                float(risultati[0]["longitude"])
            )

            cache_set(cache_key, punto, 30 * 24 * 3600)

            return punto

        cache_set(cache_key, -1, 30 * 24 * 3600)

        return None

    except Exception as exc:

        print(f"\u26a0\ufe0f Geocoding {citta}: {exc}")

        return None


def meteo_per_partita(
    evento: Dict[str, Any]
) -> Optional[Dict[str, Any]]:

    """Previsioni del giorno della partita nella citta'
    dello stadio. None = non disponibile (troppo lontana,
    venue assente o servizio non raggiungibile)."""

    competitions = evento.get(
        "competitions", []
    )

    venue = (
        competitions[0].get("venue", {})
        if competitions
        else {}
    )

    citta = (
        venue.get("address", {}).get("city")
        or ""
    ).strip()

    if not citta:
        return None

    dt = (
        evento.get("_datetime")
        or parse_datetime(
            evento.get("date", "")
        )
    )

    if not dt:
        return None

    oggi = datetime.now(timezone.utc)

    giorni = (
        dt - oggi
    ).total_seconds() / 86400

    if giorni > METEO_GG_MAX:
        return None

    giorno = dt.strftime("%Y-%m-%d")

    cache_key = (
        f"meteo_{normalizza_nome(citta)}_{giorno}"
    )

    cached = cache_get(cache_key)

    if cached is not None:
        return (
            cached
            if cached != -1
            else None
        )

    punto = _geocoda_citta(citta)

    if not punto:
        cache_set(cache_key, -1, CACHE_TTL)
        return None

    try:

        r = requests.get(
            OPEN_METEO_FORECAST,
            params={
                "latitude": punto[0],
                "longitude": punto[1],
                "daily": (
                    "precipitation_sum,"
                    "wind_speed_10m_max,"
                    "temperature_2m_max,"
                    "temperature_2m_min"
                ),
                "timezone": "auto",
                "forecast_days": METEO_GG_MAX
            },
            timeout=8
        )

        if r.status_code != 200:
            cache_set(cache_key, -1, CACHE_TTL)
            return None

        daily = r.json().get("daily", {})

        tempi = daily.get("time", [])

        if giorno not in tempi:
            cache_set(cache_key, -1, CACHE_TTL)
            return None

        i = tempi.index(giorno)

        def _val(chiave, default=None):

            v = daily.get(chiave, [])

            return (
                v[i]
                if i < len(v)
                and v[i] is not None
                else default
            )

        wx = {
            "citta": citta,
            "giorno": giorno,
            "pioggia_mm": _val("precipitation_sum", 0.0),
            "vento_kmh": _val("wind_speed_10m_max", 0.0),
            "t_max": _val("temperature_2m_max"),
            "t_min": _val("temperature_2m_min")
        }

        cache_set(cache_key, wx, 6 * 3600)

        return wx

    except Exception as exc:

        print(f"\u26a0\ufe0f Meteo {citta}: {exc}")

        cache_set(cache_key, -1, CACHE_TTL)

        return None


def format_meteo(
    wx: Optional[Dict[str, Any]]
) -> str:

    if not wx:
        return "N/D (previsioni oltre 16 giorni o venue non indicata)"

    icone = []

    pioggia = safe_float(
        wx.get("pioggia_mm")
    )

    vento = safe_float(
        wx.get("vento_kmh")
    )

    if pioggia >= 5:
        icone.append("\U0001f327\ufe0f forte pioggia")
    elif pioggia >= 1:
        icone.append("\U0001f326\ufe0f pioggia")
    elif pioggia > 0:
        icone.append("\U0001f324\ufe0f sfioffi")

    if vento >= 40:
        icone.append("\U0001f4a8 vento forte")
    elif vento >= 25:
        icone.append("\U0001f32c\ufe0f ventoso")

    t_max = wx.get("t_max")
    t_min = wx.get("t_min")

    temp = ""

    if t_max is not None and t_min is not None:
        temp = (
            f" | \U0001f321\ufe0f "
            f"{safe_float(t_min):.0f}\u2013{safe_float(t_max):.0f}\u00b0C"
        )

    dettaglio = (
        f"{safe_float(pioggia):.1f} mm, "
        f"vento {safe_float(vento):.0f} km/h"
    )

    if icone:

        return (
            f"{' '.join(icone)} "
            f"({dettaglio}{temp}) \u2014 "
            f"{html_safe(wx.get('citta', ''))}"
        )

    return (
        f"\u2705 condizioni normali "
        f"({dettaglio}{temp}) \u2014 "
        f"{html_safe(wx.get('citta', ''))}"
    )


# ============================================================
# QUOTE REALI DEL MERCATO (football-data.co.uk: CSV pubblici
# gratuiti con le quote dei principali bookmaker)
#
# Attivazione automatica: le quote delle partite future
# compaiono nei CSV qualche giorno prima del turno; durante
# le pause di calendario non ci sono righe -> sezione "N/D".
# Serve SOLO da confronto: la quota REALE sostituisce quella
# stimata nel calcolo della schedina (payout realistico).
# ============================================================

FDUK_BASE = "https://www.football-data.co.uk/mmz4281"

FDUK_CODICI = {
    "ita.1": "I1",
    "eng.1": "E0",
    "esp.1": "SP1",
    "ger.1": "D1",
    "fra.1": "F1"
}

# nomi nel CSV football-data.co.uk diversi da quelli ESPN
FDUK_ALIAS = {
    "manchester united": "man united",
    "manchester city": "man city",
    "nottingham forest": "nott m forest",
    "tottenham hotspur": "tottenham",
    "west ham united": "west ham",
    "wolverhampton wanderers": "wolves",
    "atletico madrid": "ath madrid",
    "athletic bilbao": "ath bilbao",
    "athletic club": "ath bilbao",
    "real sociedad": "sociedad",
    "espanyol": "espanol",
    "real betis": "betis",
    "celta vigo": "celta",
    "deportivo alaves": "alaves",
    "borussia monchengladbach": "borussia m gladbach",
    "paris saint germain": "paris sg",
    "borussia dortmund": "dortmund",
    "bayern monaco": "bayern munich"
}


def _fduk_url_stagione(league: str) -> str:

    oggi = datetime.now(timezone.utc)

    anno = (
        oggi.year
        if oggi.month >= 7
        else oggi.year - 1
    )

    codice = FDUK_CODICI.get(league, "")

    if not codice:
        return ""

    return (
        f"{FDUK_BASE}/"
        f"{str(anno)[-2:]}{str(anno + 1)[-2:]}/"
        f"{codice}.csv"
    )


# Alias nazionali: ESPN e The Odds API usano nomi inglesi
# (Italy, Netherlands...), questa mappa copre anche varianti
# italiane/locali per il match robusto delle quote
NAZIONALI_ALIAS = {
    "italia": "italy",
    "francia": "france",
    "germania": "germany",
    "spagna": "spain",
    "inghilterra": "england",
    "olanda": "netherlands",
    "holland": "netherlands",
    "paesi bassi": "netherlands",
    "belgio": "belgium",
    "portogallo": "portugal",
    "croazia": "croatia",
    "danimarca": "denmark",
    "svezia": "sweden",
    "norvegia": "norway",
    "svizzera": "switzerland",
    "polonia": "poland",
    "repubblica ceca": "czech republic",
    "czechia": "czech republic",
    "turchia": "turkey",
    "turkiye": "turkey",
    "ungheria": "hungary",
    "grecia": "greece",
    "scozia": "scotland",
    "galles": "wales",
    "irlanda del nord": "northern ireland",
    "israele": "israel",
    "finlandia": "finland",
    "islanda": "iceland",
    "ucraina": "ukraine",
    "bielorussia": "belarus",
    "macedonia del nord": "north macedonia",
    "lussemburgo": "luxembourg",
    "stati uniti": "united states",
    "brasile": "brazil",
    "cile": "chile",
    "messico": "mexico",
    "giappone": "japan",
    "marocco": "morocco",
    "egitto": "egypt",
    "camerun": "cameroon",
    "costa d avorio": "ivory coast",
    "arabia saudita": "saudi arabia",
    "corea del sud": "south korea",
    "sud africa": "south africa",
    "bosnia ed erzegovina": "bosnia and herzegovina",
    "slovacchia": "slovakia",
    "lettonia": "latvia",
    "lituania": "lithuania"
}


def _fduk_matcha(nome_espn: str, nome_csv: str) -> bool:

    a = normalizza_nome(nome_espn)

    a = FDUK_ALIAS.get(a, a)

    a = NAZIONALI_ALIAS.get(a, a)

    b = normalizza_nome(nome_csv)

    b = NAZIONALI_ALIAS.get(b, b)

    return (
        a == b
        or a in b
        or b in a
    )


def _fduk_righe(league: str) -> Optional[List[Dict[str, Any]]]:

    cache_key = f"fduk_{league}"

    cached = cache_get(cache_key)

    if cached is not None:
        return (
            cached
            if cached != -1
            else None
        )

    url = _fduk_url_stagione(league)

    if not url:
        return None

    try:

        r = requests.get(url, timeout=15)

        if r.status_code != 200:

            cache_set(cache_key, -1, 3600)

            return None

        testo = r.content.decode("utf-8-sig")

        import csv as _csv
        import io as _io

        righe = list(
            _csv.DictReader(
                _io.StringIO(testo)
            )
        )

        cache_set(cache_key, righe, 6 * 3600)

        return righe

    except Exception as exc:

        print(f"\u26a0\ufe0f football-data.co.uk {league}: {exc}")

        cache_set(cache_key, -1, 3600)

        return None


def _fduk_num(riga, *chiavi) -> Optional[float]:

    for k in chiavi:

        v = (
            riga.get(k) or ""
        ).strip().replace(",", ".")

        if v:

            try:
                return float(v)

            except Exception:
                pass

    return None


def quote_mercato_per_partita(
    home_name: str,
    away_name: str,
    league: str,
    data_evento: Optional[datetime]
) -> Optional[Dict[str, Any]]:

    """Quote reali del mercato per la partita (se
    gia' pubblicate nei CSV). None = non disponibili."""

    righe = _fduk_righe(league)

    if not righe:
        return None

    trovata = None

    for riga in righe:

        if riga.get("FTHG"):
            continue

        if (
            riga.get("HomeTeam")
            and riga.get("AwayTeam")
            and _fduk_matcha(
                home_name,
                riga["HomeTeam"]
            )
            and _fduk_matcha(
                away_name,
                riga["AwayTeam"]
            )
        ):

            trovata = riga

            break

    if not trovata:
        return None

    q1 = _fduk_num(trovata, "AvgH", "B365H", "PSH")
    qx = _fduk_num(trovata, "AvgD", "B365D", "PSD")
    q2 = _fduk_num(trovata, "AvgA", "B365A", "PSA")

    qover = _fduk_num(trovata, "Avg>2.5", "B365>2.5", "Max>2.5")
    qunder = _fduk_num(trovata, "Avg<2.5", "B365<2.5", "Max<2.5")

    if not (q1 and qx and q2) and not (qover and qunder):
        return None

    return {
        "q1": q1,
        "qx": qx,
        "q2": q2,
        "over25": qover,
        "under25": qunder,
        "data_csv": trovata.get("Date", "")
    }


# ------------------------------------------------------------
# FORMA xG (dal CSV corrente di football-data.co.uk)
#
# Test su 106 partite 2026-27 (entrambe le squadre con >=3
# gare xG): log loss Over2.5 forma-gol 0.6871 -> forma-xG
# 0.6833. Si applica un blend 50/50 nel modello: robusto e
# reversibile se il CSV non ha dati.
# ------------------------------------------------------------

XG_W = 0.5
XG_MIN_PARTITE = 3
XG_FINESTRA = 6


def _xg_form_da_csv(
    league: str,
    team_name: str
) -> Optional[Dict[str, Any]]:

    """Media xG fatti/subiti sulle ultime XG_FINESTRA
    partite GIOCATE della stagione corrente (dal CSV).
    None se non disponibile o meno di XG_MIN_PARTITE."""

    righe = _fduk_righe(league)

    if not righe:
        return None

    cache_key = (
        f"xg_form_{league}_"
        f"{normalizza_nome(team_name)}"
    )

    cached = cache_get(cache_key)

    if cached is not None:
        return (
            cached
            if cached != -1
            else None
        )

    partite = []

    for riga in righe:

        if not riga.get("FTHG"):
            continue

        if not (
            riga.get("HxG")
            and riga.get("AxG")
        ):
            continue

        casa = _fduk_matcha(
            team_name,
            riga.get("HomeTeam", "")
        )

        fuori = _fduk_matcha(
            team_name,
            riga.get("AwayTeam", "")
        )

        if not casa and not fuori:
            continue

        try:

            partite.append({
                "data": riga.get("Date", ""),
                "fatto": float(
                    riga["HxG"]
                    if casa
                    else riga["AxG"]
                ),
                "subito": float(
                    riga["AxG"]
                    if casa
                    else riga["HxG"]
                )
            })

        except Exception:
            continue

    if len(partite) < XG_MIN_PARTITE:

        cache_set(cache_key, -1, CACHE_TTL)

        return None

    ultime = partite[-XG_FINESTRA:]

    n = len(ultime)

    form = {
        "gf": sum(
            p["fatto"] for p in ultime
        ) / n,
        "gs": sum(
            p["subito"] for p in ultime
        ) / n,
        "n": n
    }

    cache_set(cache_key, form)

    return form


def _applica_xg(
    stats: Dict[str, Any],
    xg: Optional[Dict[str, Any]]
) -> Dict[str, Any]:

    """Blend 50/50 tra forma-gol e forma-xG per i soli
    valori di attacco/difesa usati dal modello."""

    if (
        not xg
        or xg.get("n", 0) < XG_MIN_PARTITE
    ):
        return stats

    w = XG_W

    return {
        **stats,
        "gf": (
            w * xg["gf"]
            + (1 - w)
            * safe_float(stats.get("gf"))
        ),
        "gs": (
            w * xg["gs"]
            + (1 - w)
            * safe_float(stats.get("gs"))
        )
    }


# ============================================================
# NOTIZIE REALI DI SQUADRA (Google News RSS, gratuito,
# senza chiave - testato da IP datacenter)
#
# Mostra i TITOLI reali: nessuna probabilita' inventata.
# Le notizie con parole chiave (infortuni, squalifiche,
# operazioni...) alzano un avviso "da verificare" sia nel
# report sia accanto alle gambe delle schedine.
# ============================================================

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

NOTIZIE_FLAG_PAROLE = (
    "infortun",
    "squalif",
    "operazion",
    "influenz",
    "indagat",
    "condann",
    "rottura",
    "stop per",
    "fuori per"
)

NOTIZIE_MAX_ORE = 96


def notizie_squadra(
    team_name: str
) -> Optional[Dict[str, Any]]:

    """Ultime notizie della squadra (max 3, ultime 96h)
    + flag se compaiono parole chiave sensibili."""

    cache_key = (
        f"notizie_{normalizza_nome(team_name)}"
    )

    cached = cache_get(cache_key)

    if cached is not None:
        return (
            cached
            if cached != -1
            else None
        )

    try:

        r = requests.get(
            GOOGLE_NEWS_RSS,
            params={
                "q": f'"{team_name}" calcio',
                "hl": "it",
                "gl": "IT",
                "ceid": "IT:it"
            },
            timeout=8
        )

        if r.status_code != 200:

            cache_set(cache_key, -1, 1800)

            return None

        import xml.etree.ElementTree as ET

        root = ET.fromstring(r.content)

        notizie = []

        for item in root.findall(".//item")[:10]:

            titolo = (
                item.findtext("title")
                or ""
            ).strip()

            fonte = (
                item.findtext("source")
                or ""
            ).strip()

            if fonte and titolo.endswith(
                f" - {fonte}"
            ):
                titolo = titolo[
                    :-len(f" - {fonte}")
                ].strip()

            ore = None

            data_raw = item.findtext(
                "pubDate"
            )

            if data_raw:

                try:

                    from email.utils import (
                        parsedate_to_datetime
                    )

                    dt = parsedate_to_datetime(
                        data_raw
                    )

                    ore = (
                        datetime.now(timezone.utc)
                        - dt
                    ).total_seconds() / 3600

                except Exception:
                    ore = None

            if ore is not None and ore > NOTIZIE_MAX_ORE:
                continue

            if not titolo:
                continue

            notizie.append({
                "titolo": titolo[:110],
                "fonte": fonte,
                "ore": (
                    round(ore)
                    if ore is not None
                    else None
                )
            })

            if len(notizie) >= 3:
                break

        flag = any(
            any(
                p in n["titolo"].lower()
                for p in NOTIZIE_FLAG_PAROLE
            )
            for n in notizie
        )

        risultato = {
            "notizie": notizie,
            "flag": flag
        }

        cache_set(cache_key, risultato, 3600)

        return risultato

    except Exception as exc:

        print(f"\u26a0\ufe0f Notizie {team_name}: {exc}")

        cache_set(cache_key, -1, 1800)

        return None


def format_notizie(
    n: Optional[Dict[str, Any]]
) -> str:

    if n is None:
        return "N/D (servizio notizie non raggiungibile)"

    lista = n.get("notizie", [])

    if not lista:
        return "Nessuna notizia recente"

    righe = []

    for item in lista:

        testo = f"\u2022 {html_safe(item['titolo'])}"

        extra = []

        if item.get("fonte"):
            extra.append(
                html_safe(item["fonte"])
            )

        if item.get("ore") is not None:
            extra.append(f"{item['ore']}h")

        if extra:
            testo += f" ({', '.join(extra)})"

        righe.append(testo)

    if n.get("flag"):
        righe.append(
            "\u26a0\ufe0f rilevate notizie sensibili "
            "(infortuni/squalifiche): verifica "
            "prima di giocare la partita"
        )

    return "\n".join(righe)


# ============================================================
# EUROPA
# ============================================================

def competizione_europea(
    evento: Dict[str, Any]
) -> Optional[str]:

    slug = slug_competizione(
        evento
    )

    if slug in COMPETIZIONI_EUROPEE:
        return COMPETIZIONI_EUROPEE[slug]

    if slug.startswith(
        "uefa."
    ):

        league_obj = evento.get(
            "league",
            {}
        )

        return (
            league_obj.get(
                "name"
            )
            or slug
        )

    return None


def verifica_impegni_europei(
    team_id: Optional[str]
) -> Dict[str, Any]:

    if not team_id:

        return {
            "stato": "non_disponibile",
            "competizioni": [],
            "ultima": None
        }

    oggi = datetime.now(
        timezone.utc
    )

    calendario = storia_squadra(
        team_id,
        oggi
    )

    if calendario is None:

        return {
            "stato": "non_disponibile",
            "competizioni": [],
            "ultima": None
        }

    limite = (
        oggi
        - timedelta(days=45)
    )

    europee = []

    for evento in calendario:

        if not evento_terminato(
            evento,
            oggi
        ):
            continue

        dt = parse_datetime(
            evento.get(
                "date",
                ""
            )
        )

        if not dt:
            continue

        if dt < limite:
            continue

        competizione = (
            competizione_europea(
                evento
            )
        )

        if competizione:

            europee.append({
                "date": dt,
                "competizione": competizione,
                "evento": evento
            })

    europee.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    competizioni = []

    for item in europee:

        nome = item[
            "competizione"
        ]

        if nome not in competizioni:
            competizioni.append(
                nome
            )

    ultima = (
        europee[0]
        if europee
        else None
    )

    return {
        "stato": "ok",
        "competizioni": competizioni,
        "ultima": ultima
    }


# ============================================================
# RIPOSO / FATICA
# ============================================================

def giorni_dall_ultima_partita(
    form: List[Dict[str, Any]]
) -> Optional[float]:

    if not form:
        return None

    ultima = max(
        (
            x["date"]
            for x in form
            if x.get("date")
        ),
        default=None
    )

    if not ultima:
        return None

    oggi = datetime.now(
        timezone.utc
    )

    delta = oggi - ultima

    return max(
        0,
        delta.total_seconds()
        / 86400
    )


def riposo_reale(
    form: List[Dict[str, Any]],
    data_evento: Optional[datetime]
) -> Optional[float]:

    """Giorni di riposo PRIMA della partita da pronosticare:
    differenza tra la data dell'evento e l'ultima gara
    giocata. (Prima si usava 'oggi': durante le pause di
    calendario questo dava fatica fittizia al 100%.)"""

    if not form:
        return None

    ultima = max(
        (
            x["date"]
            for x in form
            if x.get("date")
        ),
        default=None
    )

    if not ultima:
        return None

    if not data_evento:
        data_evento = datetime.now(
            timezone.utc
        )

    delta = (
        data_evento - ultima
    ).total_seconds() / 86400

    return max(
        0,
        delta
    )


def indice_fatica(
    giorni: Optional[float]
) -> int:

    if giorni is None:
        return 0

    if giorni >= 7:
        return 0

    if giorni >= 5:
        return 10

    if giorni >= 4:
        return 20

    if giorni >= 3:
        return 35

    if giorni >= 2:
        return 55

    return 70


# ============================================================
# MOMENTUM
# ============================================================

def calcola_momentum(
    statistiche: Dict[str, Any]
) -> int:

    ppg = safe_float(
        statistiche.get(
            "ppg"
        )
    )

    gf = safe_float(
        statistiche.get(
            "gf"
        )
    )

    gs = safe_float(
        statistiche.get(
            "gs"
        )
    )

    differenza = gf - gs

    punteggio = 0

    if ppg >= 2.5:
        punteggio += 25

    elif ppg >= 2.1:
        punteggio += 18

    elif ppg >= 1.7:
        punteggio += 10

    elif ppg >= 1.3:
        punteggio += 0

    elif ppg >= 0.9:
        punteggio -= 10

    else:
        punteggio -= 18

    if differenza >= 1.0:
        punteggio += 8

    elif differenza >= 0.4:
        punteggio += 4

    elif differenza <= -1.0:
        punteggio -= 8

    elif differenza <= -0.4:
        punteggio -= 4

    return int(
        clamp(
            punteggio,
            -25,
            30
        )
    )


# ============================================================
# PROBABILITÀ
# ============================================================

def calcola_probabilita(
    home_name: str,
    away_name: str,
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float],
    fatigue_home: int,
    fatigue_away: int,
    h2h: List[Dict[str, Any]],
    ppg_campionato_home: Optional[float] = None,
    ppg_campionato_away: Optional[float] = None
) -> Tuple[float, float, float]:

    # --------------------------------------------------------
    # Pesi calibrati con backtest su 1000 partite delle 5
    # grandi leghe (stagione 2025-26). Vedere backtest.py:
    # log loss -3.5% rispetto ai pesi originali, coerente
    # in-sample e out-of-sample.
    # --------------------------------------------------------

    home = 45.0
    draw = 27.0
    away = 28.0

    home_ppg = safe_float(
        stats_home.get(
            "ppg"
        )
    )

    away_ppg = safe_float(
        stats_away.get(
            "ppg"
        )
    )

    # --------------------------------------------------------
    # Forma generale (tutte le competizioni)
    # --------------------------------------------------------

    differenza_forma = (
        home_ppg
        - away_ppg
    )

    home += (
        differenza_forma * 4
    )

    away -= (
        differenza_forma * 4
    )

    # --------------------------------------------------------
    # Forma di solo campionato (ultime 8 in lega)
    # --------------------------------------------------------

    if (
        ppg_campionato_home is not None
        and ppg_campionato_away is not None
    ):

        differenza_lega = (
            ppg_campionato_home
            - ppg_campionato_away
        )

        home += (
            differenza_lega * 3
        )

        away -= (
            differenza_lega * 3
        )

    # --------------------------------------------------------
    # Rendimento casa / trasferta
    # --------------------------------------------------------

    if ppg_casa is not None:

        home += (
            ppg_casa - 1.5
        ) * 5

    if ppg_trasferta is not None:

        away += (
            ppg_trasferta - 1.5
        ) * 5

    # --------------------------------------------------------
    # Fatica (il vantaggio campo è già nella base 45/27/28
    # e nel rendimento casa/trasferta: nessun bonus extra)
    # --------------------------------------------------------

    home -= (
        fatigue_home * 0.04
    )

    away -= (
        fatigue_away * 0.04
    )

    # --------------------------------------------------------
    # Momentum
    # --------------------------------------------------------

    momentum_home = (
        calcola_momentum(
            stats_home
        )
    )

    momentum_away = (
        calcola_momentum(
            stats_away
        )
    )

    home += (
        momentum_home
        - momentum_away
    ) * 0.2

    away += (
        momentum_away
        - momentum_home
    ) * 0.2

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h_home = 0
    h2h_draw = 0
    h2h_away = 0

    home_norm = normalizza_nome(
        home_name
    )

    for match in h2h:

        mh = normalizza_nome(
            match["home"]
        )

        ma = normalizza_nome(
            match["away"]
        )

        hg = match["home_score"]
        ag = match["away_score"]

        if mh == home_norm:

            gol_home = hg
            gol_away = ag

        elif ma == home_norm:

            gol_home = ag
            gol_away = hg

        else:
            continue

        if gol_home > gol_away:
            h2h_home += 1

        elif gol_home < gol_away:
            h2h_away += 1

        else:
            h2h_draw += 1

    totale_h2h = (
        h2h_home
        + h2h_draw
        + h2h_away
    )

    if totale_h2h > 0:

        peso = min(
            8,
            totale_h2h * 1.5
        )

        quota_home = (
            h2h_home
            / totale_h2h
        )

        quota_draw = (
            h2h_draw
            / totale_h2h
        )

        quota_away = (
            h2h_away
            / totale_h2h
        )

        home += (
            quota_home * peso
            - peso / 3
        )

        draw += (
            quota_draw * peso
            - peso / 3
        )

        away += (
            quota_away * peso
            - peso / 3
        )

    # --------------------------------------------------------
    # Equilibrio
    # --------------------------------------------------------

    differenza_assoluta = abs(
        home - away
    )

    if differenza_assoluta < 5:
        draw += 4

    elif differenza_assoluta < 10:
        draw += 2

    home = max(
        home,
        1
    )

    draw = max(
        draw,
        1
    )

    away = max(
        away,
        1
    )

    totale = (
        home
        + draw
        + away
    )

    # shrinkage di calibrazione verso la distribuzione
    # empirica 1X2 (vedi CAL_*)
    w = CAL_W_1X2
    b1, bx, b2 = CAL_BASE_1X2

    home_c = (
        w * (home / totale * 100)
        + (1 - w) * b1
    )

    draw_c = (
        w * (draw / totale * 100)
        + (1 - w) * bx
    )

    away_c = (
        w * (away / totale * 100)
        + (1 - w) * b2
    )

    tot_c = (
        home_c + draw_c + away_c
    )

    return (
        home_c / tot_c * 100,
        draw_c / tot_c * 100,
        away_c / tot_c * 100
    )


# ============================================================
# MERCATI AVANZATI (matrice Poisson bivariata)
#
# Tutti i mercati "dis_derivati" del listino bookmaker sono
# calcolati dalla stessa matrice dei punteggi esatti:
#   U/O e Goal, Asiatiche/Handicap (linee con mezzo gol),
#   Primo/Secondo Tempo, Casa/Ospite (gol squadra),
#   Ris.Esatto, 1T/Finale, Combo, Multigol (anche con 1X2/DC),
#   Multibet Tempo, Speciali Gol/Minuti, combo U/O.
# Validazione su 250 partite 2026-27 (football-data.co.uk):
#   Over 2.5 matrice 58.7% vs reale 58.8%, Esatto 1-1 10.9 vs
#   10.0, GG 60.4 vs 56.8 (max scarto ~4pp).
# ESCLUSI: Sanzioni/cartellini (nessuna fonte gratuita
# affidabile) e linee asiatiche con rimborso (0.25/0.75:
# stake parziali non rappresentabili a quota fissa).
# ============================================================

QUOTA_1T = 0.42
# ~42% dei gol arriva nel primo tempo (319/761 gol su 250
# partite delle 5 grandi leghe 2026-27, fduk)

MATRICE_N = 9


def stima_goal_squadre(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float]
) -> Tuple[float, float]:

    """Lambda di gol attesi SEPARATI per squadra (stessa
    logica interna di stima_goal, ma senza sommarli)."""

    home_gf = safe_float(
        stats_home.get("gf")
    )

    home_gs = safe_float(
        stats_home.get("gs")
    )

    away_gf = safe_float(
        stats_away.get("gf")
    )

    away_gs = safe_float(
        stats_away.get("gs")
    )

    expected_home = (
        home_gf * 0.55
        + away_gs * 0.45
    )

    expected_away = (
        away_gf * 0.55
        + home_gs * 0.45
    )

    if ppg_casa is not None:

        expected_home += (
            ppg_casa - 1.5
        ) * 0.15

    if ppg_trasferta is not None:

        expected_away += (
            ppg_trasferta - 1.5
        ) * 0.15

    expected_home = clamp(
        expected_home,
        0.15,
        3.5
    )

    expected_away = clamp(
        expected_away,
        0.15,
        3.5
    )

    return expected_home, expected_away


def matrice_score(
    lh: float,
    la: float
) -> List[List[float]]:

    """Matrice P(i gol casa, j gol ospite), Poisson
    indipendenti troncata a MATRICE_N-1 e rinormalizzata."""

    mat = [
        [
            poisson_prob(lh, i)
            * poisson_prob(la, j)
            for j in range(MATRICE_N)
        ]
        for i in range(MATRICE_N)
    ]

    s = sum(sum(riga) for riga in mat)

    if s <= 0:
        return mat

    return [
        [v / s for v in riga]
        for riga in mat
    ]


def _distr_tot(
    mat: List[List[float]]
) -> List[float]:

    """Distribuzione dei gol TOTALI (0..2N-2)."""

    d = [0.0] * (2 * MATRICE_N - 1)

    for i in range(MATRICE_N):

        for j in range(MATRICE_N):

            d[i + j] += mat[i][j]

    return d


def mercati_avanzati(
    lh: float,
    la: float
):

    """Costruisce TUTTI i mercati derivati dal modello.
    Ritorna (mercati_dict, esatto_top, htft_top):
    - mercati_dict: {label: probabilita%} per il pool
    - esatto_top / htft_top: liste [(label, %)] per il report
    """

    tot = lh + la

    if tot < 0.5:
        f = 0.5 / tot
        lh *= f
        la *= f

    elif tot > 6.0:
        f = 6.0 / tot
        lh *= f
        la *= f

    m = matrice_score(lh, la)

    m1 = matrice_score(
        lh * QUOTA_1T,
        la * QUOTA_1T
    )

    m2 = matrice_score(
        lh * (1 - QUOTA_1T),
        la * (1 - QUOTA_1T)
    )

    d = _distr_tot(m)
    d1 = _distr_tot(m1)
    d2 = _distr_tot(m2)

    def somma(mat, cond):

        return sum(
            mat[i][j]
            for i in range(MATRICE_N)
            for j in range(MATRICE_N)
            if cond(i, j)
        )

    # ---- U/O e Goal ----
    over_gol = somma(
        m, lambda i, j:
        i >= 1 and j >= 1 and i + j >= 3
    )

    under_gol = somma(
        m, lambda i, j:
        i >= 1 and j >= 1 and i + j <= 2
    )

    over_nogol = somma(
        m, lambda i, j:
        (i == 0 and j >= 3)
        or (j == 0 and i >= 3)
    )

    # ---- margini (handicap / asiatiche) ----
    casa_2 = somma(m, lambda i, j: i - j >= 2)
    casa_3 = somma(m, lambda i, j: i - j >= 3)
    osp_2 = somma(m, lambda i, j: j - i >= 2)
    osp_3 = somma(m, lambda i, j: j - i >= 3)

    merc = {
        # U/O e Goal
        "Over 2.5 & Gol": over_gol * 100,
        "Under 2.5 & Gol": under_gol * 100,
        "Over 2.5 & No Gol": over_nogol * 100,
        # Handicap (linee rette, senza rimborso)
        "Handicap Casa -1.5": casa_2 * 100,
        "Handicap Ospite -1.5": osp_2 * 100,
        # Asiatiche (mezzi gol: mai rimborso)
        "Asiatica Casa +1.5": (1 - osp_2) * 100,
        "Asiatica Ospite +1.5": (1 - casa_2) * 100,
        "Asiatica Casa +2.5": (1 - osp_3) * 100,
        "Asiatica Ospite +2.5": (1 - casa_3) * 100,
        # Primo Tempo
        "1T Over 0.5": (1 - d1[0]) * 100,
        "1T Under 1.5": (d1[0] + d1[1]) * 100,
        "1T Gol": somma(
            m1, lambda i, j: i >= 1 and j >= 1
        ) * 100,
        # Secondo Tempo
        "2T Over 0.5": (1 - d2[0]) * 100,
        "2T Over 1.5": (
            1 - d2[0] - d2[1]
        ) * 100,
        "2T Gol": somma(
            m2, lambda i, j: i >= 1 and j >= 1
        ) * 100,
        # Casa / Ospite (gol di squadra)
        "Casa Over 0.5": somma(
            m, lambda i, j: i >= 1
        ) * 100,
        "Casa Over 1.5": somma(
            m, lambda i, j: i >= 2
        ) * 100,
        "Ospite Over 0.5": somma(
            m, lambda i, j: j >= 1
        ) * 100,
        "Ospite Over 1.5": somma(
            m, lambda i, j: j >= 2
        ) * 100,
        # Combo
        "1 & Gol": somma(
            m, lambda i, j:
            i > j and j >= 1
        ) * 100,
        "2 & Over 1.5": somma(
            m, lambda i, j:
            j > i and i + j >= 2
        ) * 100,
        # Multigol (gol totali nel range, inclusi)
        "Multigol 1-3": sum(d[1:4]) * 100,
        "Multigol 2-4": sum(d[2:5]) * 100,
        "Multigol 1-4": sum(d[1:5]) * 100,
        "Multigol 2-5": sum(d[2:6]) * 100,
        "Multigol 3-6": sum(d[3:7]) * 100,
        # 1X2 + Multigol
        "1 & Multigol 1-3": somma(
            m, lambda i, j:
            i > j and 1 <= i + j <= 3
        ) * 100,
        "2 & Multigol 2-4": somma(
            m, lambda i, j:
            j > i and 2 <= i + j <= 4
        ) * 100,
        # DC + Multigol
        "1X & Multigol 1-4": somma(
            m, lambda i, j:
            i >= j and 1 <= i + j <= 4
        ) * 100,
        "X2 & Multigol 2-4": somma(
            m, lambda i, j:
            j >= i and 2 <= i + j <= 4
        ) * 100,
        # Multibet Tempo
        "1T Ovr 0.5 & Fin Ovr 2.5": sum(
            d1[a] * sum(d2[max(0, 3 - a):])
            for a in range(1, 2 * MATRICE_N - 1)
        ) * 100,
        # Combo Multigol Casa + Osp.
        "MultiCasa 1+ & Osp 1-3": somma(
            m, lambda i, j:
            i >= 1 and 1 <= j <= 3
        ) * 100,
        # Combo Multigol 1T + 2T
        "Multi 1T 1+ & 2T 1-3": (
            (1 - d1[0]) * sum(d2[1:4])
        ) * 100,
        # Speciali Gol
        "Gol 1T e 2T": (
            (1 - d1[0]) * (1 - d2[0])
        ) * 100,
        "Casa segna 1T e 2T": (
            (1 - somma(m1, lambda i, j: i == 0))
            * (1 - somma(m2, lambda i, j: i == 0))
        ) * 100,
        "Ospite segna 1T e 2T": (
            (1 - somma(m1, lambda i, j: j == 0))
            * (1 - somma(m2, lambda i, j: j == 0))
        ) * 100,
        # Combo U/O 1T + 2T
        "U 1.5 1T & O 0.5 2T": (
            (d1[0] + d1[1]) * (1 - d2[0])
        ) * 100,
        "O 0.5 1T & O 1.5 2T": (
            (1 - d1[0]) * (1 - d2[0] - d2[1])
        ) * 100,
        # Combo U/O Casa + Ospite
        "Casa O 0.5 & Osp U 1.5": somma(
            m, lambda i, j:
            i >= 1 and j <= 1
        ) * 100,
        "Casa U 1.5 & Osp O 0.5": somma(
            m, lambda i, j:
            i <= 1 and j >= 1
        ) * 100,
        # Speciali Minuti (timing ~ uniforme nel 1T)
        "Gol entro il 30'": (
            1 - math.exp(
                -QUOTA_1T * tot * (29.0 / 45.0)
            )
        ) * 100
    }

    merc = {
        k: clamp(v, 1.0, 99.0)
        for k, v in merc.items()
    }

    # ---- Ris. Esatto (top 2) ----
    punteggi = sorted(
        (
            (i, j, m[i][j])
            for i in range(MATRICE_N)
            for j in range(MATRICE_N)
        ),
        key=lambda t: -t[2]
    )

    esatto_top = [
        (f"{i}-{j}", p * 100)
        for i, j, p in punteggi
        if p >= 0.06
    ][:2]

    # ---- 1T/Finale (top 2) ----
    def _esito(x, y):

        if x > y:
            return "1"

        if x == y:
            return "X"

        return "2"

    htft = {}

    for i1 in range(MATRICE_N):

        for j1 in range(MATRICE_N):

            if m1[i1][j1] < 0.001:
                continue

            r1 = _esito(i1, j1)

            for i2 in range(MATRICE_N):

                for j2 in range(MATRICE_N):

                    if m2[i2][j2] < 0.001:
                        continue

                    k = (
                        r1
                        + "/"
                        + _esito(i1 + i2, j1 + j2)
                    )

                    htft[k] = (
                        htft.get(k, 0.0)
                        + m1[i1][j1] * m2[i2][j2]
                    )

    htft_ord = sorted(
        htft.items(),
        key=lambda kv: -kv[1]
    )

    htft_top = [
        (k, v * 100)
        for k, v in htft_ord[:2]
        if v >= 0.08
    ]

    # le 1T/Finale piu' probabili entrano anche nel pool
    # (solo se plausibili per le fasce basse: >= 18%)
    for k, v in htft_ord[:2]:

        if v >= 0.18:
            merc[f"1T/F {k}"] = clamp(
                v * 100, 1.0, 99.0
            )

    return merc, esatto_top, htft_top


def format_esatto(
    analisi: Dict[str, Any]
) -> str:

    top = analisi.get("esatto_top") or []

    if not top:
        return "N/D"

    return " - ".join(
        f"{s} ({p:.0f}%)"
        for s, p in top
    )


def format_htft(
    analisi: Dict[str, Any]
) -> str:

    top = analisi.get("htft_top") or []

    if not top:
        return "N/D"

    return " - ".join(
        f"{s} ({p:.0f}%)"
        for s, p in top
    )


# ============================================================
# GOAL ATTESI
# ============================================================

def stima_goal(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float]
) -> float:

    home_gf = safe_float(
        stats_home.get(
            "gf"
        )
    )

    home_gs = safe_float(
        stats_home.get(
            "gs"
        )
    )

    away_gf = safe_float(
        stats_away.get(
            "gf"
        )
    )

    away_gs = safe_float(
        stats_away.get(
            "gs"
        )
    )

    expected_home = (
        home_gf * 0.55
        + away_gs * 0.45
    )

    expected_away = (
        away_gf * 0.55
        + home_gs * 0.45
    )

    if ppg_casa is not None:

        expected_home += (
            ppg_casa - 1.5
        ) * 0.15

    if ppg_trasferta is not None:

        expected_away += (
            ppg_trasferta - 1.5
        ) * 0.15

    expected_home = clamp(
        expected_home,
        0.15,
        3.5
    )

    expected_away = clamp(
        expected_away,
        0.15,
        3.5
    )

    totale = (
        expected_home
        + expected_away
    )

    return clamp(
        totale,
        0.5,
        6.0
    )


def poisson_prob(
    lamb: float,
    k: int
) -> float:

    try:

        return (
            math.exp(-lamb)
            * lamb ** k
            / math.factorial(k)
        )

    except Exception:
        return 0.0


def probabilita_over(
    expected_goals: float,
    soglia: int
) -> float:

    """Probabilita' totale gol >= soglia (soglia=k =>
    'Over k.5'), con shrinkage calibrato per gradino
    (CAL_SCALA_W / CAL_SCALA_BASE, backtest 1000 p.)."""

    under = 0.0

    for i in range(soglia):

        under += poisson_prob(
            expected_goals,
            i
        )

    over = clamp(
        (1 - under) * 100,
        0.5,
        99.5
    )

    w = CAL_SCALA_W.get(
        soglia,
        0.35
    )

    base = CAL_SCALA_BASE.get(
        soglia,
        52.9
    )

    return clamp(
        w * over
        + (1 - w) * base,
        1,
        99
    )


def probabilita_over_25(
    expected_goals: float
) -> float:

    return probabilita_over(
        expected_goals,
        2
    )


def probabilita_gol(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any]
) -> float:

    home_gf = safe_float(
        stats_home.get(
            "gf"
        )
    )

    home_gs = safe_float(
        stats_home.get(
            "gs"
        )
    )

    away_gf = safe_float(
        stats_away.get(
            "gf"
        )
    )

    away_gs = safe_float(
        stats_away.get(
            "gs"
        )
    )

    attacco = (
        home_gf
        + away_gf
    ) / 2

    difesa = (
        home_gs
        + away_gs
    ) / 2

    indice = (
        attacco * 28
        + difesa * 20
    )

    # il mercato Gol/BTTS del modello non ha skill
    # oltre la frequenza base (verificato sul
    # backtest): shrinkage forte verso la base
    return clamp(
        CAL_W_GOL * indice
        + (1 - CAL_W_GOL)
        * CAL_BASE_GOL,
        1,
        99
    )


# ============================================================
# AFFIDABILITÀ
# ============================================================

def calcola_affidabilita(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    injuries_home: Optional[
        List[Dict[str, Any]]
    ],
    injuries_away: Optional[
        List[Dict[str, Any]]
    ],
    h2h: List[Dict[str, Any]],
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float],
    lp_home: Optional[float] = None,
    lp_away: Optional[float] = None
) -> int:

    score = 45

    # Forma
    if stats_home.get(
        "partite",
        0
    ) >= 5:

        score += 5

    if stats_away.get(
        "partite",
        0
    ) >= 5:

        score += 5

    # Casa / trasferta
    if ppg_casa is not None:
        score += 3

    if ppg_trasferta is not None:
        score += 3

    # Forma di solo campionato disponibile
    if lp_home is not None and lp_away is not None:
        score += 2

    # H2H
    if len(h2h) >= 3:
        score += 3

    # Infortuni
    if injuries_home is not None:
        score += 2

    if injuries_away is not None:
        score += 2

    return int(
        clamp(
            score,
            25,
            85
        )
    )


# ============================================================
# ANALISI SINGOLA PARTITA
# ============================================================

def analizza_partita(
    evento: Dict[str, Any],
    league: str,
    indice: int,
    totale: int
) -> Optional[Dict[str, Any]]:

    home, away = (
        estrai_competitors(
            evento
        )
    )

    if not home or not away:
        return None

    home_name = estrai_nome_team(
        home
    )

    away_name = estrai_nome_team(
        away
    )

    home_id = estrai_id_team(
        home
    )

    away_id = estrai_id_team(
        away
    )

    print(
        f"🔎 Analisi {indice}/{totale}: "
        f"{home_name} - {away_name}"
    )

    # --------------------------------------------------------
    # FORMA (storia squadra: tutte le competizioni)
    # --------------------------------------------------------

    form_home = (
        recupera_form_da_eventi(
            home_name,
            league,
            home_id
        )
    )

    form_away = (
        recupera_form_da_eventi(
            away_name,
            league,
            away_id
        )
    )

    stats_home = (
        statistiche_form(
            form_home,
            home_name
        )
    )

    stats_away = (
        statistiche_form(
            form_away,
            away_name
        )
    )

    # --------------------------------------------------------
    # FORMA xG (blend nel modello, il report resta sui gol)
    # --------------------------------------------------------

    xg_home = _xg_form_da_csv(
        league,
        home_name
    )

    xg_away = _xg_form_da_csv(
        league,
        away_name
    )

    stats_model_home = _applica_xg(
        stats_home,
        xg_home
    )

    stats_model_away = _applica_xg(
        stats_away,
        xg_away
    )

    # --------------------------------------------------------
    # CASA / TRASFERTA
    # --------------------------------------------------------

    ppg_casa = rendimento_casa(
        form_home,
        home_name
    )

    ppg_trasferta = (
        rendimento_trasferta(
            form_away,
            away_name
        )
    )

    # --------------------------------------------------------
    # FORMA DI SOLO CAMPIONATO (ultime 8 in lega)
    # --------------------------------------------------------

    lp_home = ppg_campionato_squadra(
        home_id,
        home_name,
        league
    )

    lp_away = ppg_campionato_squadra(
        away_id,
        away_name,
        league
    )

    # --------------------------------------------------------
    # INFORTUNI (football-data.org se configurata la chiave,
    # fallback ESPN)
    # --------------------------------------------------------

    data_evento = (
        evento.get("_datetime")
        or parse_datetime(
            evento.get("date", "")
        )
    )

    (
        fd_inj_home,
        fd_inj_away
    ) = recupera_infortuni_fd(
        data_evento,
        home_name,
        away_name,
        league
    )

    injuries_home = (
        fd_inj_home
        if fd_inj_home is not None
        else recupera_infortuni(
            home_id,
            league
        )
    )

    injuries_away = (
        fd_inj_away
        if fd_inj_away is not None
        else recupera_infortuni(
            away_id,
            league
        )
    )

    # --------------------------------------------------------
    # H2H (tutte le competizioni, 2 stagioni)
    # --------------------------------------------------------

    h2h = recupera_h2h(
        home_name,
        away_name,
        home_id,
        away_id,
        league
    )

    # --------------------------------------------------------
    # EUROPA
    # --------------------------------------------------------

    europe_home = (
        verifica_impegni_europei(
            home_id
        )
    )

    europe_away = (
        verifica_impegni_europei(
            away_id
        )
    )

    # --------------------------------------------------------
    # METEO (informativo, non altera le probabilita')
    # --------------------------------------------------------

    wx = meteo_per_partita(
        evento
    )

    # --------------------------------------------------------
    # QUOTE REALI DEL MERCATO (football-data.co.uk)
    # --------------------------------------------------------

    quote_mercato = (
        quote_mercato_per_partita(
            home_name,
            away_name,
            league,
            data_evento
        )
    )

    # fallback: quote REALI da The Odds API (se
    # configurata la chiave) per competizioni senza
    # CSV fduk (nazionali, coppe) o quando fduk non
    # ha ancora pubblicato le quote del turno
    if quote_mercato is None:

        sport_odds = ODDS_API_SPORT.get(
            league
        )

        if sport_odds:

            quote_mercato = (
                odds_api_per_partita(
                    sport_odds,
                    home_name,
                    away_name,
                    data_evento
                )
            )











    # --------------------------------------------------------
    # RIPOSO / FATICA
    # --------------------------------------------------------

    riposo_home = (
        riposo_reale(
            form_home,
            data_evento
        )
    )

    riposo_away = (
        riposo_reale(
            form_away,
            data_evento
        )
    )

    fatica_home = indice_fatica(
        riposo_home
    )

    fatica_away = indice_fatica(
        riposo_away
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum_home = (
        calcola_momentum(
            stats_model_home
        )
    )

    momentum_away = (
        calcola_momentum(
            stats_model_away
        )
    )

    # --------------------------------------------------------
    # PROBABILITÀ
    # --------------------------------------------------------

    (
        prob_home,
        prob_draw,
        prob_away
    ) = calcola_probabilita(
        home_name,
        away_name,
        stats_model_home,
        stats_model_away,
        ppg_casa,
        ppg_trasferta,
        fatica_home,
        fatica_away,
        h2h,
        lp_home,
        lp_away
    )

    # --------------------------------------------------------
    # GOAL
    # --------------------------------------------------------

    expected_goals = stima_goal(
        stats_model_home,
        stats_model_away,
        ppg_casa,
        ppg_trasferta
    )

    over25 = (
        probabilita_over_25(
            expected_goals
        )
    )

    under25 = 100 - over25

    gol = probabilita_gol(
        stats_model_home,
        stats_away
    )

    # mercati avanzati dalla matrice dei punteggi
    lh_av, la_av = stima_goal_squadre(
        stats_model_home,
        stats_model_away,
        ppg_casa,
        ppg_trasferta
    )

    avanzati, esatto_top, htft_top = mercati_avanzati(
        lh_av,
        la_av
    )

    # --------------------------------------------------------
    # AFFIDABILITÀ
    # --------------------------------------------------------

    affidabilita = (
        calcola_affidabilita(
            stats_home,
            stats_away,
            injuries_home,
            injuries_away,
            h2h,
            ppg_casa,
            ppg_trasferta,
            lp_home,
            lp_away
        )
    )

    # --------------------------------------------------------
    # PRONOSTICO MIGLIORE
    # --------------------------------------------------------

    mercati = {
        "1": prob_home,
        "X": prob_draw,
        "2": prob_away,
        "Over 2.5": over25,
        "Under 2.5": under25,
        "Gol": gol,
        "No Gol": 100 - gol
    }

    migliore = max(
        mercati,
        key=mercati.get
    )

    return {
        "evento": evento,
        "home": home_name,
        "away": away_name,
        "home_id": home_id,
        "away_id": away_id,
        "form_home": form_home,
        "form_away": form_away,
        "stats_home": stats_home,
        "stats_away": stats_away,
        "ppg_casa": ppg_casa,
        "ppg_trasferta": ppg_trasferta,
        "lp_home": lp_home,
        "lp_away": lp_away,
        "injuries_home": injuries_home,
        "injuries_away": injuries_away,
        "h2h": h2h,
        "europe_home": europe_home,
        "europe_away": europe_away,
        "mercati_avanzati": avanzati,
        "esatto_top": esatto_top,
        "htft_top": htft_top,
        "meteo": wx,
        "quote_mercato": quote_mercato,
        "xg_home": (
            stats_model_home is not stats_home
        ),
        "xg_away": (
            stats_model_away is not stats_away
        ),





        "riposo_home": riposo_home,
        "riposo_away": riposo_away,
        "fatica_home": fatica_home,
        "fatica_away": fatica_away,
        "momentum_home": momentum_home,
        "momentum_away": momentum_away,
        "prob_home": prob_home,
        "prob_draw": prob_draw,
        "prob_away": prob_away,
        "expected_goals": expected_goals,
        "over15": probabilita_over(
            expected_goals,
            1
        ),
        "over35": probabilita_over(
            expected_goals,
            3
        ),
        "under15": 100 - probabilita_over(
            expected_goals,
            1
        ),
        "under35": 100 - probabilita_over(
            expected_goals,
            3
        ),
        "under45": 100 - probabilita_over(
            expected_goals,
            4
        ),
        "over25": over25,
        "under25": under25,
        "gol": gol,
        "no_gol": 100 - gol,
        "migliore": migliore,
        "migliore_valore": mercati[
            migliore
        ],
        "affidabilita": affidabilita
    }


# ============================================================
# FORMATTAZIONE EUROPA
# ============================================================

def format_europa(
    europa: Dict[str, Any]
) -> str:

    if not europa:
        return "⚠️ dati non disponibili"

    stato = europa.get(
        "stato"
    )

    if stato != "ok":
        return "⚠️ dati non disponibili"

    competizioni = europa.get(
        "competizioni",
        []
    )

    if not competizioni:
        return (
            "Nessun impegno europeo recente"
        )

    return ", ".join(
        html_safe(c)
        for c in competizioni
    )


# ============================================================
# QUOTE REALI DA THE ODDS API (opzionale, chiave env)
#
# football-data.co.uk pubblica quote reali solo per i 5
# campionati club e pochi giorni prima del turno. Per
# nazionali (Nations League) e coppe (Champions, Europa,
# Conference) -- e per i club in attesa di pubblicazione --
# se e' configurata la variabile env THE_ODDS_API_KEY
# (gratuita su the-odds-api.com, 500 crediti/mese) il bot
# usa le quote REALI dei bookmaker (la migliore tra le
# piattaforme EU) nello stesso identico flusso dei club:
# quota "(reale X.XX)" nelle gambe e ⭐ se il modello
# trova valore rispetto al mercato.
# Costo: 2 crediti per competizione per chiamata
# (h2h + totals, regione eu), cache 2h / negativa 30min.
# Senza chiave: tutto funziona come prima (quote stimate).
# ============================================================

THE_ODDS_API_KEY = os.getenv(
    "THE_ODDS_API_KEY",
    ""
).strip()

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

ODDS_API_SPORT = {
    "uefa.nations": (
        "soccer_uefa_nations_league"
    ),
    "uefa.champions": (
        "soccer_uefa_champions_league"
    ),
    "uefa.europa": (
        "soccer_uefa_europa_league"
    ),
    "uefa.europa.conf": (
        "soccer_uefa_europa_conference_league"
    ),
    "ita.1": "soccer_italy_serie_a",
    "eng.1": "soccer_epl",
    "esp.1": "soccer_spain_la_liga",
    "ger.1": "soccer_germany_bundesliga",
    "fra.1": "soccer_france_ligue_one"
}


def _odds_api_eventi(
    sport: str
) -> Optional[List[Dict[str, Any]]]:

    """Elenco eventi con quote (h2h + totals, migliori
    bookmaker EU) per la competizione. Cache 2h,
    negativa 30 min. None se chiave assente/errore."""

    if not THE_ODDS_API_KEY:
        return None

    cache_key = f"oddsapi_{sport}"

    cached = cache_get(cache_key)

    if cached is not None:
        return (
            cached
            if cached != -1
            else None
        )

    try:

        r = requests.get(
            f"{ODDS_API_BASE}/sports/{sport}/odds",
            params={
                "apiKey": THE_ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h,totals",
                "oddsFormat": "decimal"
            },
            timeout=10
        )

        if r.status_code != 200:

            print(
                f"\u26a0\ufe0f Odds API {sport}: "
                f"HTTP {r.status_code}"
            )

            cache_set(cache_key, -1, 1800)

            return None

        eventi = r.json() or []

        cache_set(cache_key, eventi, 7200)

        return eventi

    except Exception as exc:

        print(f"\u26a0\ufe0f Odds API {sport}: {exc}")

        cache_set(cache_key, -1, 1800)

        return None


def _odds_api_migliori(
    evento: Dict[str, Any]
) -> Dict[str, float]:

    """Migliore quota decimale per ogni esito tra
    tutti i bookmaker dell'evento. Solo linea
    Over/Under 2.5 per i totals."""

    best: Dict[str, float] = {}

    for book in (
        evento.get("bookmakers") or []
    ):

        for market in (
            book.get("markets") or []
        ):

            mk = market.get("key")

            for out in (
                market.get("outcomes") or []
            ):

                nome = str(
                    out.get("name", "")
                )

                prezzo = safe_float(
                    out.get("price")
                )

                if not prezzo or prezzo <= 1:
                    continue

                if mk == "h2h":

                    chiave = {
                        "Home": "q1",
                        "Draw": "qx",
                        "Away": "q2"
                    }.get(nome)

                elif mk == "totals":

                    punto = safe_float(
                        out.get("point")
                    )

                    if (
                        punto is None
                        or abs(punto - 2.5) > 0.01
                    ):
                        continue

                    if nome == "Over":
                        chiave = "over25"

                    elif nome == "Under":
                        chiave = "under25"

                    else:
                        chiave = None

                else:
                    chiave = None

                if not chiave:
                    continue

                if prezzo > best.get(chiave, 0):
                    best[chiave] = prezzo

    return best


def odds_api_per_partita(
    sport: str,
    home_name: str,
    away_name: str,
    data_evento: Any
) -> Optional[Dict[str, Any]]:

    """Quote reali del match (se presente nell'elenco
    della competizione) nello stesso formato di
    quote_mercato_per_partita. Match per nome squadre
    (fuzzy) + orario within 20h."""

    eventi = _odds_api_eventi(sport)

    if not eventi:
        return None

    if isinstance(
        data_evento,
        datetime
    ):

        t_evento = data_evento

        if t_evento.tzinfo is None:
            t_evento = t_evento.replace(
                tzinfo=timezone.utc
            )

    else:

        try:

            t_evento = datetime.fromisoformat(
                str(data_evento).replace(
                    "Z",
                    "+00:00"
                )
            )

        except Exception:
            return None

    for ev in eventi:

        try:

            t_ev = datetime.fromisoformat(
                str(
                    ev.get("commence_time", "")
                ).replace("Z", "+00:00")
            )

        except Exception:
            continue

        if abs(
            (t_ev - t_evento).total_seconds()
        ) > 20 * 3600:
            continue

        if not (
            _fduk_matcha(
                home_name,
                str(ev.get("home_team", ""))
            )
            and _fduk_matcha(
                away_name,
                str(ev.get("away_team", ""))
            )
        ):
            continue

        best = _odds_api_migliori(ev)

        if (
            best.get("q1")
            and best.get("qx")
            and best.get("q2")
        ):

            return {
                "q1": best["q1"],
                "qx": best["qx"],
                "q2": best["q2"],
                "over25": best.get(
                    "over25"
                ),
                "under25": best.get(
                    "under25"
                ),
                "data_csv": None,
                "fonte": "the-odds-api"
            }

    return None


def format_quote_mercato(
    q: Optional[Dict[str, Any]]
) -> str:

    if not q:
        return (
            "N/D (il mercato non ha ancora "
            "pubblicato le quote: di norma "
            "arrivano pochi giorni prima del turno)"
        )

    righe = []

    if q.get("q1") and q.get("qx") and q.get("q2"):

        tot = (
            1.0 / q["q1"]
            + 1.0 / q["qx"]
            + 1.0 / q["q2"]
        )

        righe.append(
            f"1X2 reali: {q['q1']:.2f} / "
            f"{q['qx']:.2f} / {q['q2']:.2f}"
        )

        righe.append(
            f"Probabilit\u00e0 di mercato: "
            f"{100 / (q['q1'] * tot):.0f}% / "
            f"{100 / (q['qx'] * tot):.0f}% / "
            f"{100 / (q['q2'] * tot):.0f}%"
        )

    if q.get("over25") and q.get("under25"):

        righe.append(
            f"Over/Under 2.5 reali: "
            f"{q['over25']:.2f} / "
            f"{q['under25']:.2f}"
        )

    if q.get("fonte") == "the-odds-api":

        righe.append(
            "Fonte: migliori quote bookmaker "
            "EU (The Odds API)"
        )

    return "\n".join(righe)


# ============================================================
# FORMATTAZIONE REPORT
# ============================================================

def format_report_partita(
    analisi: Dict[str, Any],
    numero: int
) -> str:

    home = html_safe(
        analisi["home"]
    )

    away = html_safe(
        analisi["away"]
    )

    sh = analisi["stats_home"]
    sa = analisi["stats_away"]

    injuries_h = (
        analisi["injuries_home"]
    )

    injuries_a = (
        analisi["injuries_away"]
    )

    h2h = analisi["h2h"]

    ppg_casa = (
        analisi["ppg_casa"]
    )

    ppg_trasferta = (
        analisi["ppg_trasferta"]
    )

    riposo_h = (
        analisi["riposo_home"]
    )

    riposo_a = (
        analisi["riposo_away"]
    )

    def ppg_text(value):

        if value is None:
            return "N/D"

        return f"{value:.2f}"

    def lp_text(value):

        if value is None:
            return "N/D"

        return f"{value:.2f}"

    xg_line_home = (
        "Forma xG: inclusa nel modello"
        if analisi.get("xg_home")
        else ""
    )

    xg_line_away = (
        "Forma xG: inclusa nel modello"
        if analisi.get("xg_away")
        else ""
    )

    def riposo_text(value):

        if value is None:
            return "N/D"

        return f"{value:.1f} giorni"

    def momentum_text(value):

        if value > 0:
            return f"+{value}"

        return str(value)

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    if h2h:

        righe_h2h = []

        for match in h2h[:3]:

            data = (
                match["date"]
                .strftime(
                    "%d/%m/%Y"
                )
            )

            righe_h2h.append(
                f"• {data}: "
                f"{html_safe(match['home'])} "
                f"{match['home_score']}-"
                f"{match['away_score']} "
                f"{html_safe(match['away'])}"
            )

        h2h_text = "\n".join(
            righe_h2h
        )

    else:

        h2h_text = (
            "Nessun confronto recente "
            "disponibile"
        )

    # --------------------------------------------------------
    # Migliore
    # --------------------------------------------------------

    migliore = analisi[
        "migliore"
    ]

    migliore_valore = analisi[
        "migliore_valore"
    ]

    testo = f"""
<b>{numero}. {home} - {away}</b>

━━━━━━━━━━━━━━━━━━

<b>📈 FORMA RECENTE</b>

{home}: {sh['sequenza']}
PPG {sh['ppg']:.2f} | GF {sh['gf']:.2f} | GS {sh['gs']:.2f}
PPG campionato: {lp_text(analisi['lp_home'])}
{xg_line_home}

{away}: {sa['sequenza']}
PPG {sa['ppg']:.2f} | GF {sa['gf']:.2f} | GS {sa['gs']:.2f}
PPG campionato: {lp_text(analisi['lp_away'])}
{xg_line_away}

<b>🏠 RENDIMENTO CASA / TRASFERTA</b>

{home} in casa: {ppg_text(ppg_casa)} PPG
{away} in trasferta: {ppg_text(ppg_trasferta)} PPG

<b>⏱ RIPOSO / FATICA</b>

{home}: {riposo_text(riposo_h)}
Indice fatica: {analisi['fatica_home']}/70

{away}: {riposo_text(riposo_a)}
Indice fatica: {analisi['fatica_away']}/70

<b>🌍 IMPEGNI EUROPEI</b>

{home}: {format_europa(analisi['europe_home'])}

{away}: {format_europa(analisi['europe_away'])}

<b>🏥 INFORTUNI</b>

{home}:
{format_infortuni(injuries_h)}

{away}:
{format_infortuni(injuries_a)}

<b>🌦 METEO (stadio)</b>

{format_meteo(analisi['meteo'])}

<b>📰 NOTIZIE</b>

{home}:
{format_notizie(analisi.get('notizie_home'))}

{away}:
{format_notizie(analisi.get('notizie_away'))}

<b>🔥 MOMENTUM</b>

{home}: {momentum_text(analisi['momentum_home'])}
{away}: {momentum_text(analisi['momentum_away'])}

<b>⚔️ H2H RECENTE</b>

{h2h_text}

<b>🎯 PROBABILITÀ 1X2</b>

1 — {format_percent(analisi['prob_home'])}
X — {format_percent(analisi['prob_draw'])}
2 — {format_percent(analisi['prob_away'])}

<b>⚽ GOAL</b>

Over 2.5: {format_percent(analisi['over25'])}
Under 2.5: {format_percent(analisi['under25'])}

Gol: {format_percent(analisi['gol'])}
No Gol: {format_percent(analisi['no_gol'])}

Goal attesi: <b>{analisi['expected_goals']:.2f}</b>

<b>💰 QUOTA REALE (mercato)</b>

{format_quote_mercato(analisi.get('quote_mercato'))}

<b>⭐ PRONOSTICO PRINCIPALE</b>

<b>{migliore}</b> — {format_percent(migliore_valore)}

<b>🎯 RIS. ESATTO & 1T/FINALE</b>

Esatto: {format_esatto(analisi)}
1T/Finale: {format_htft(analisi)}

<b>📊 AFFIDABILITÀ DATI</b>

{analisi['affidabilita']}%

━━━━━━━━━━━━━━━━━━
"""

    return testo.strip()


# ============================================================
# CREAZIONE REPORT
# ============================================================

def crea_report(
    campionato_key: str,
    on_progress: Optional[
        Any
    ] = None
) -> Optional[str]:

    configurazione = config_competizione(
        campionato_key
    )

    if configurazione is None:
        return None

    league = configurazione[
        "espn"
    ]

    nome_campionato = configurazione[
        "nome"
    ]

    print()
    print("=" * 50)
    print(
        f"📨 RICHIESTA CAMPIONATO: "
        f"{nome_campionato}"
    )
    print("=" * 50)

    partite = (
        recupera_partite_future(
            league
        )
    )

    if not partite:

        return (
            f"⚠️ Non sono state trovate "
            f"partite future per "
            f"<b>{html_safe(nome_campionato)}</b> "
            f"nei prossimi 42 giorni "
            f"(possibile pausa di calendario)."
        )

    partite = partite[
        :NUM_PARTITE_REPORT
    ]

    print(
        f"📋 Partite da analizzare: "
        f"{len(partite)}"
    )

    analisi_completa = []

    for indice, evento in enumerate(
        partite,
        start=1
    ):

        try:

            analisi = analizza_partita(
                evento,
                league,
                indice,
                len(partite)
            )

            if analisi:

                analisi_completa.append(
                    analisi
                )

                analisi["notizie_home"] = (
                    notizie_squadra(
                        analisi["home"]
                    )
                )

                analisi["notizie_away"] = (
                    notizie_squadra(
                        analisi["away"]
                    )
                )

                if on_progress:
                    on_progress(
                        indice,
                        len(partite),
                        analisi["home"],
                        analisi["away"]
                    )

        except Exception as exc:

            print(
                f"❌ Errore analisi "
                f"partita {indice}: "
                f"{exc}"
            )

    print()

    print(
        f"🏁 ANALISI COMPLETATE: "
        f"{len(analisi_completa)}/"
        f"{len(partite)}"
    )

    if not analisi_completa:

        return (
            f"⚠️ Nessun pronostico valido "
            f"generato per "
            f"{html_safe(nome_campionato)}."
        )

    print(
        f"📊 Pronostici validi creati: "
        f"{len(analisi_completa)}"
    )

    parti = []

    intestazione = f"""
<b>⚽ BOT PRONOSTICI CALCIO</b>

<b>{html_safe(nome_campionato)}</b>

📅 Analisi delle prossime partite

<i>Le percentuali sono stime statistiche basate sui dati disponibili e non rappresentano garanzie di risultato.</i>

━━━━━━━━━━━━━━━━━━
""".strip()

    parti.append(
        intestazione
    )

    for numero, analisi in enumerate(
        analisi_completa,
        start=1
    ):

        parti.append(
            format_report_partita(
                analisi,
                numero
            )
        )

    # --------------------------------------------------------
    # RIEPILOGO FINALE
    # --------------------------------------------------------

    migliori = sorted(
        analisi_completa,
        key=lambda x: (
            x["migliore_valore"],
            x["affidabilita"]
        ),
        reverse=True
    )

    righe = [
        "<b>📌 RIEPILOGO PRONOSTICI</b>",
        ""
    ]

    for analisi in migliori:

        righe.append(
            f"• <b>"
            f"{html_safe(analisi['home'])} - "
            f"{html_safe(analisi['away'])}"
            f"</b>"
        )

        righe.append(
            f"  "
            f"{analisi['migliore']} "
            f"{format_percent(analisi['migliore_valore'])}"
        )

    righe.append("")

    righe.append(
        "⚠️ <i>Utilizzare i dati come "
        "supporto statistico e non come "
        "garanzia di vincita.</i>"
    )

    righe.append(
        "ℹ️ <i>Mercati avanzati (1T/2T, handicap, "
        "multigol, esatti): stime Poisson calibrate "
        "su 250 partite 2026-27. Sanzioni/cartellini "
        "esclusi: nessuna fonte gratuita "
        "affidabile.</i>"
    )

    parti.append(
        "\n".join(righe)
    )

    report = "\n\n".join(
        parti
    )

    print(
        "📨 Report finale creato."
    )

    print(
        f"📏 Lunghezza report: "
        f"{len(report)} caratteri"
    )

    print(
        "✅ CREA_REPORT TERMINATO "
        "CORRETTAMENTE."
    )

    return report


# ============================================================
# INVIO REPORT TELEGRAM
# ============================================================

def invia_report(
    chat_id: int,
    report: str
):

    print(
        "📨 INVIO REPORT TELEGRAM"
    )

    print(
        f"📏 Lunghezza totale: "
        f"{len(report)} caratteri"
    )

    MAX_LEN = 3800

    messaggi = []

    testo = report

    while len(testo) > MAX_LEN:

        posizione = testo.rfind(
            "\n\n",
            0,
            MAX_LEN
        )

        if posizione < 1000:

            posizione = testo.rfind(
                "\n",
                0,
                MAX_LEN
            )

        if posizione < 1000:
            posizione = MAX_LEN

        messaggi.append(
            testo[:posizione]
        )

        testo = testo[
            posizione:
        ].lstrip()

    if testo:
        messaggi.append(
            testo
        )

    print(
        f"📨 Messaggi da inviare: "
        f"{len(messaggi)}"
    )

    for indice, messaggio in enumerate(
        messaggi,
        start=1
    ):

        print(
            f"📤 Invio messaggio "
            f"{indice}/"
            f"{len(messaggi)} "
            f"({len(messaggio)} caratteri)"
        )

        try:

            bot.send_message(
                chat_id,
                messaggio,
                disable_web_page_preview=True
            )

            print(
                f"✅ Messaggio "
                f"{indice}/"
                f"{len(messaggi)} inviato."
            )

        except Exception as exc:

            print(
                f"❌ Errore invio "
                f"messaggio {indice}: "
                f"{exc}"
            )

            try:

                testo_pulito = re.sub(
                    r"<[^>]+>",
                    "",
                    messaggio
                )

                bot.send_message(
                    chat_id,
                    testo_pulito,
                    disable_web_page_preview=True,
                    parse_mode=None
                )

                print(
                    f"✅ Fallback messaggio "
                    f"{indice} inviato."
                )

            except Exception as exc2:

                print(
                    f"❌ Fallback fallito: "
                    f"{exc2}"
                )

    print(
        "🏁 REPORT TELEGRAM INVIATO "
        "COMPLETAMENTE."
    )



# ============================================================
# SCHEDINE
#
# Tre schedine pronte costruite sulle partite con affidabilità
# più alta di TUTTI i campionati in elenco:
#   🎟 SICURA      quota totale ≤ 10 (2-3 eventi)
#   ⚡ EQUILIBRATA quota totale ≤ 25 (2-4 eventi)
#   🔥 AUDACE      quota totale ≤ 50 (3-6 eventi)
# Per ogni partita si sceglie il mercato con probabilità più
# alta; la quota è stimata con payout 94%. Le combinazioni
# vengono scelte massimizzando la probabilità complessiva
# entro la fascia di quota.
# ============================================================

try:
    from zoneinfo import ZoneInfo

    TZ_ROMA = ZoneInfo(
        "Europe/Rome"
    )

except Exception:

    TZ_ROMA = timezone(
        timedelta(hours=1)
    )

GIORNI_SETTIMANA = [
    "lun",
    "mar",
    "mer",
    "gio",
    "ven",
    "sab",
    "dom"
]


def quota_stimata(
    probabilita: float
) -> float:

    prob = clamp(
        safe_float(probabilita),
        1.0,
        99.0
    )

    return max(
        1.01,
        round(
            QUOTA_PAYOUT
            * 100.0
            / prob,
            2
        )
    )


def data_ora_italiana(
    dt: Optional[datetime]
) -> str:

    if not dt:
        return "data N/D"

    try:

        locale = dt.astimezone(
            TZ_ROMA
        )

    except Exception:
        locale = dt

    giorno = GIORNI_SETTIMANA[
        locale.weekday()
    ]

    return (
        f"{giorno} "
        f"{locale.strftime('%d/%m %H:%M')}"
    )


def migliore_pick(
    analisi: Dict[str, Any]
) -> Dict[str, Any]:

    mercati = {
        "1": analisi["prob_home"],
        "X": analisi["prob_draw"],
        "2": analisi["prob_away"],
        "Over 2.5": analisi["over25"],
        "Under 2.5": analisi["under25"],
        "Gol": analisi["gol"],
        "No Gol": analisi["no_gol"]
    }

    mercato = max(
        mercati,
        key=mercati.get
    )

    prob = safe_float(
        mercati[mercato],
        1.0
    )

    return {
        "analisi": analisi,
        "mercato": mercato,
        "prob": clamp(
            prob,
            1.0,
            99.0
        ),
        "quota": quota_stimata(
            prob
        ),
        "aff": safe_int(
            analisi.get(
                "affidabilita"
            )
        )
    }


def migliori_pick(
    analisi: Dict[str, Any],
    max_pick: int = 2
) -> List[Dict[str, Any]]:

    """Fino a max_pick mercati giocabili per la partita
    (il piu' probabile per primo). Piu' varianti per
    partita = piu' quote disponibili per riempire le
    fasce delle schedine. Ogni pick porta il match_key
    per vietare due eventi della stessa partita in una
    sola schedina.
    """

    tot_1x2 = max(
        1.0,
        analisi["prob_home"]
        + analisi["prob_draw"]
        + analisi["prob_away"]
    )

    mercati = {
        "1": analisi["prob_home"],
        "X": analisi["prob_draw"],
        "2": analisi["prob_away"],
        "Over 2.5": analisi["over25"],
        "Under 2.5": analisi["under25"],
        # scala O/U calibrata: gradini intermedi per
        # costruire quote con meno rischio per gamba
        "Over 1.5": analisi["over15"],
        "Under 4.5": analisi["under45"],
        "Under 3.5": analisi["under35"],
        "Over 3.5": analisi["over35"],
        "Under 1.5": analisi["under15"],
        "Gol": analisi["gol"],
        "No Gol": analisi["no_gol"],
        # doppie chance (da 1X2 calibrato): gambe a
        # probabilita' alta per le schedine prudenti
        "Doppia 1X": (
            (analisi["prob_home"]
             + analisi["prob_draw"])
            / tot_1x2 * 100
        ),
        "Doppia 12": (
            DC12_W
            * (
                (analisi["prob_home"]
                 + analisi["prob_away"])
                / tot_1x2 * 100
            )
            + (1 - DC12_W)
            * DC12_BASE
        ),
        "Doppia X2": (
            (analisi["prob_draw"]
             + analisi["prob_away"])
            / tot_1x2 * 100
        )
    }

    avanzati = analisi.get(
        "mercati_avanzati"
    ) or {}

    mercati.update(avanzati)

    ordine = sorted(
        mercati.items(),
        key=lambda kv: kv[1],
        reverse=True
    )[:max(1, max_pick)]

    match_key = str(
        analisi.get("evento", {}).get("id", "")
    ) or (
        f"{analisi.get('home','')}#"
        f"{analisi.get('away','')}#"
        f"{analisi.get('evento', {}).get('date','')}"
    )

    # quote reali del mercato (se pubblicate) per i
    # mercati coperti dai CSV di football-data.co.uk
    reali = {}

    qm = analisi.get("quote_mercato")

    if qm:

        reali = {
            "1": qm.get("q1"),
            "X": qm.get("qx"),
            "2": qm.get("q2"),
            "Over 2.5": qm.get("over25"),
            "Under 2.5": qm.get("under25")
        }

    tot_1 = sum(
        1.0 / reali[k]
        for k in ("1", "X", "2")
        if reali.get(k)
    ) or None

    tot_ou = (
        1.0 / reali["Over 2.5"]
        + 1.0 / reali["Under 2.5"]
        if reali.get("Over 2.5")
        and reali.get("Under 2.5")
        else None
    )

    picks = []

    for mercato, prob in ordine:

        prob = clamp(
            safe_float(prob, 1.0),
            1.0,
            99.0
        )

        quota = quota_stimata(prob)

        qr = reali.get(mercato)

        implied = None

        if qr:

            if mercato in ("1", "X", "2") and tot_1:
                implied = 100.0 / (qr * tot_1)

            elif mercato == "Over 2.5" and tot_ou:
                implied = 100.0 / (qr * tot_ou)

            elif mercato == "Under 2.5" and tot_ou:
                implied = 100.0 / (qr * tot_ou)

            # la quota REALE e' quella che verremmo
            # pagati: sostituisce la stima
            quota = safe_float(qr, quota)

        picks.append({
            "analisi": analisi,
            "mercato": mercato,
            "prob": prob,
            "quota": quota,
            "quota_reale": qr,
            "implied_reale": implied,
            "aff": safe_int(
                analisi.get("affidabilita")
            ),
            "match_key": match_key
        })

    return picks


TIERS_SCHEDINE = [
    {
        "nome": "🎟 SCHEDINA SICURA",
        "cap": 10.0,
        "min_legs": 2,
        "max_legs": 3,
        "floor": 1.5,
        "candidati": 34,
        "prob_min": 0.40,
        "no_fatica": True
    },
    {
        "nome": "⚡ SCHEDINA EQUILIBRATA",
        "cap": 25.0,
        "min_legs": 2,
        "max_legs": 4,
        "floor": 4.0,
        "candidati": 34,
        "prob_min": 0.40,
        "no_fatica": True
    },
    {
        "nome": "🔥 SCHEDINA AUDACE",
        "cap": 50.0,
        "min_legs": 3,
        "max_legs": 6,
        "floor": 15.0,
        "candidati": 34,
        "prob_min": 0.22
    },
    {
        "nome": "💣 SCHEDINA MITO",
        "cap": 60.0,
        "min_legs": 4,
        "max_legs": 6,
        "floor": 50.0,
        "candidati": 34,
        "prob_min": 0.20
    }
]


def _mito_estesa(
    tier: Dict[str, Any],
    combo: List[Dict[str, Any]],
    quota_tot: float,
    picks_ordinate: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:

    """MITO a 8 GAMBE garantite. Con 8 gambe la quota
    media per gamba deve essere ~2.1 per superare il
    pavimento 50: servono gambe GROSSE (2-4.5), non
    solo quelle economiche. Per ogni partita del pool
    considero la gamba economica + la grossa + la piu'
    probabile, scelgo le 20 partite piu' utili e cerco
    le combinazioni di 8 (partite distinte) con quota
    in [floor, cap] massimizzando la probabilita'.
    Se matematicamente impossibile (pool corto o
    quote tutte piccole) restituisce None."""

    filtrati = [
        p for p in picks_ordinate
        if p["prob"] / 100.0
        >= tier["prob_min"]
    ]

    # per ogni partita: gamba economica + grossa
    # (max quota, serve a raggiungere 50+) + la
    # piu' probabile
    by_match: Dict[str, List[Dict[str, Any]]] = {}

    for p in filtrati:

        by_match.setdefault(
            p["match_key"], []
        ).append(p)

    per_match: Dict[
        str, List[Dict[str, Any]]
    ] = {}

    for mk, lista in (
        by_match.items()
    ):

        scelte = []

        economica = min(
            lista,
            key=lambda p: p["quota"]
        )

        scelte.append(economica)

        grossa = max(
            lista,
            key=lambda p: p["quota"]
        )

        if (
            grossa is not economica
            and grossa["quota"] <= 6.0
        ):

            scelte.append(grossa)

        probabile = max(
            lista,
            key=lambda p: p["prob"]
        )

        if probabile not in scelte:
            scelte.append(probabile)

        per_match[mk] = scelte

    # selezione delle partite piu' utili: le 10 con
    # la gamba economica piu' bassa (fattibilita')
    # + le 10 con la gamba grossa piu' alta
    # (raggiungimento della fascia 50-60)
    ord_econ = sorted(
        per_match.keys(),
        key=lambda mk: min(
            p["quota"]
            for p in per_match[mk]
        )
    )[:10]

    ord_grossa = sorted(
        per_match.keys(),
        key=lambda mk: -max(
            p["quota"]
            for p in per_match[mk]
        )
    )[:10]

    visti_m2 = set()
    match_keys = []

    for mk in ord_econ + ord_grossa:

        if mk in visti_m2:
            continue

        visti_m2.add(mk)
        match_keys.append(mk)

    if len(match_keys) < 8:
        return None

    best = None
    best_score = None

    # prima prova a chiudere nella parte alta della
    # finestra (>= 53), poi scende al pavimento
    for soglia in (53.0, tier["floor"]):

        for otto in itertools.combinations(
            match_keys,
            8
        ):

            # tutte le combinazioni di gambe (1-3
            # per partita) delle 8 partite scelte
            for quote in itertools.product(
                *[
                    per_match[mk]
                    for mk in otto
                ]
            ):

                quota_tot8 = 1.0

                for p in quote:

                    quota_tot8 *= p["quota"]

                if quota_tot8 > tier["cap"]:
                    continue

                if quota_tot8 < soglia:
                    continue

                score = 0.0

                for p in quote:

                    score += math.log(
                        p["prob"] / 100.0
                    )

                if (
                    best_score is None
                    or score > best_score
                ):

                    best_score = score
                    best = list(quote)

        if best:
            break

    if not best:
        return None

    aff_media = sum(
        p["aff"] for p in best
    ) / len(best)

    prob_combinata = (
        100.0
        * math.prod(
            p["prob"] / 100.0
            for p in best
        )
    )

    return {
        "tier": tier,
        "legs": best,
        "quota_tot": round(
            math.prod(
                p["quota"] for p in best
            ),
            2
        ),
        "aff_media": round(
            aff_media
        ),
        "prob_combinata": round(
            prob_combinata,
            1
        )
    }


def costruisci_schedina(
    tier: Dict[str, Any],
    picks_ordinate: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:

    candidati = [
        p for p in picks_ordinate
        if p["prob"] / 100.0
        >= tier["prob_min"]
    ]

    # squadre stanche (riposo < 3 giorni) fuori
    # dalle schedine prudenti
    if tier.get("no_fatica"):

        candidati = [
            p for p in candidati
            if max(
                safe_int(
                    p["analisi"].get(
                        "fatica_home"
                    )
                ),
                safe_int(
                    p["analisi"].get(
                        "fatica_away"
                    )
                )
            ) < 55
        ]

    # Selezione bilanciata: meta' delle gambe piu'
    # PROBABILI (per non buttare fuori le doppie
    # chance alte) + meta' delle gambe a QUOTA piu'
    # alta (per raggiungere le fasce delle schedine
    # alte come l'AUDACE).
    per_prob = sorted(
        candidati,
        key=lambda p: -p["prob"]
    )[:tier["candidati"] // 2]

    per_quota = sorted(
        candidati,
        key=lambda p: -p["quota"]
    )[:tier["candidati"] // 2]

    # GRUPPO MID: la parte centrale della scala quote
    # (1.35-2.19) e' quella che consente di riempire
    # le fasce con il minor rischio per gamba: senza
    # questo gruppo resta un buco tra top-probabilita'
    # (1.03-1.14) e top-quota (2.2+)
    mid = [
        p for p in candidati
        if 1.35 <= p["quota"] <= 2.19
    ]

    mid = sorted(
        mid,
        key=lambda p: -p["prob"]
    )[:10]

    visti_s = set()
    candidati = []

    for p in per_prob + mid + per_quota:

        k = (
            p["match_key"],
            p["mercato"]
        )

        if k in visti_s:
            continue

        visti_s.add(k)

        candidati.append(p)

    # NUOVA LOGICA FASCE: prima la fascia target
    # [floor, cap]; solo se NON esiste nessuna
    # combinazione in fascia si scende (floor*0.5,
    # poi qualunque quota sotto cap). Dentro la
    # prima fascia non vuota vince il punteggio di
    # probabilita' piu' alto.
    fasce = [
        tier["floor"],
        tier["floor"] * 0.5,
        0.0
    ]

    best = None

    for floor in fasce:

        best_fase = None
        best_score = None

        for size in range(
            tier["min_legs"],
            tier["max_legs"] + 1
        ):

            if size > len(candidati):
                break

            for combo in itertools.combinations(
                candidati,
                size
            ):

                # mai due eventi della stessa partita
                chiavi = {
                    p["match_key"]
                    for p in combo
                }

                if len(chiavi) != size:
                    continue

                quota_tot = 1.0
                score = 0.0

                for p in combo:

                    quota_tot *= p[
                        "quota"
                    ]

                    score += math.log(
                        p["prob"]
                        / 100.0
                    )

                if quota_tot > tier["cap"]:
                    continue

                if quota_tot < floor:
                    continue

                if (
                    best_score is None
                    or score > best_score
                ):

                    best_score = score
                    best_fase = (
                        combo,
                        quota_tot
                    )

        if best_fase:
            best = best_fase
            break

    if not best:
        return None

    combo, quota_tot = best

    # MITO: se la combinazione migliore resta sul
    # pavimento (~50) si ripete la ricerca con piu'
    # GAMBE (7-8) per spingere la quota in [53, cap];
    # se non esiste nulla di meglio resta questa.
    if (
        tier["cap"] >= 59.5
        and len(combo) < 8
        and tier["max_legs"] <= 6
    ):

        estesa = _mito_estesa(
            tier,
            combo,
            quota_tot,
            picks_ordinate
        )

        if estesa:
            return estesa

    aff_media = sum(
        p["aff"] for p in combo
    ) / len(combo)

    prob_combinata = (
        100.0
        * math.prod(
            p["prob"] / 100.0
            for p in combo
        )
    )

    return {
        "tier": tier,
        "legs": list(combo),
        "quota_tot": round(
            quota_tot,
            2
        ),
        "aff_media": round(
            aff_media
        ),
        "prob_combinata": round(
            prob_combinata,
            1
        )
    }


def format_schedina(
    schedina: Dict[str, Any]
) -> str:

    tier = schedina["tier"]

    righe = [
        f"<b>{tier['nome']}</b>",
        (
            f"Quota massima: "
            f"{tier['cap']:.0f}"
        ),
        "",
        (
            f"<b>Quota totale stimata: "
            f"{schedina['quota_tot']:.2f}</b>"
        ),
        (
            f"Probabilità complessiva: "
            f"~{schedina['prob_combinata']:.1f}%"
        ),
        (
            f"Affidabilità media dati: "
            f"{schedina['aff_media']}%"
        ),
        "",
        "<b>EVENTI</b>"
    ]

    for numero, leg in enumerate(
        schedina["legs"],
        start=1
    ):

        analisi = leg["analisi"]

        home = html_safe(
            analisi["home"]
        )

        away = html_safe(
            analisi["away"]
        )

        lega = html_safe(
            CAMPIONATI.get(
                campionato_da_espn(
                    analisi.get(
                        "league",
                        ""
                    )
                ),
                {}
            ).get(
                "nome",
                ""
            )
        )

        dt = (
            analisi["evento"].get(
                "_datetime"
            )
            or parse_datetime(
                analisi["evento"].get(
                    "date",
                    ""
                )
            )
        )

        righe.append(
            ""
        )

        righe.append(
            f"<b>{numero}. {home} - {away}</b>"
        )

        extra = []

        if lega:
            extra.append(lega)

        quando = data_ora_italiana(
            dt
        )

        if quando != "data N/D":
            extra.append(quando)

        wx_leg = analisi.get("meteo")

        if wx_leg:

            pioggia = safe_float(
                wx_leg.get("pioggia_mm")
            )

            vento = safe_float(
                wx_leg.get("vento_kmh")
            )

            meteo_txt = (
                f"\U0001f326 {pioggia:.0f}mm "
                f"{vento:.0f}km/h"
                if pioggia >= 1
                else f"\u2705 {vento:.0f}km/h"
            )

            extra.append(meteo_txt)

        if extra:
            righe.append(
                " | ".join(extra)
            )

        extra_leg = ""

        if leg.get("quota_reale"):

            extra_leg = (
                f" (reale {leg['quota_reale']:.2f})"
            )

            imp = leg.get("implied_reale")

            if (
                imp
                and leg["prob"]
                > imp + 5
            ):
                extra_leg += " \u2b50"

        righe.append(
            f"Pronostico: "
            f"<b>{html_safe(leg['mercato'])}</b> "
            f"({leg['prob']:.0f}%) "
            f"\u2014 quota {leg['quota']:.2f}"
            f"{extra_leg} "
            f"| affidabilit\u00e0 {leg['aff']}%"
        )

        if leg.get("news_flag"):
            righe.append(
                "\u26a0\ufe0f\U0001f4f0 notizie da "
                "verificare su una delle due squadre"
            )

    righe.append("")
    righe.append(
        "⚠️ <i>Quote stimate dal modello "
        "(payout 94%), non quote di un "
        "bookmaker. Le percentuali sono "
        "stime statistiche e non garantiscono "
        "alcun risultato. Gioca sempre in "
        "modo responsabile.</i>"
    )

    return "\n".join(righe)


def crea_schedine(
    cap: Optional[float] = None,
    on_progress: Optional[Any] = None,
    slugs: Optional[List[str]] = None
) -> Optional[List[str]]:

    print()
    print("=" * 50)
    print("🎟 RICHIESTA SCHEDINE (cap: "
          + (str(cap) if cap else "tutte")
          + ")")
    print("=" * 50)

    pool = []

    if slugs:

        leghe = [
            (
                slug,
                config_competizione(slug)
                or {
                    "espn": slug,
                    "nome": slug
                }
            )
            for slug in slugs
        ]

    else:

        leghe = list(
            CAMPIONATI.items()
        )

    totali = 0
    fatti = 0

    per_lega = []

    def _partite(pair):

        league = pair[1]["espn"]

        try:
            return league, (
                recupera_partite_future(
                    league
                )
            )

        except Exception as exc:
            print(
                f"⚠️ Partite {league}: {exc}"
            )

            return league, []

    # 5 campionati IN PARALLELO
    with ThreadPoolExecutor(
        max_workers=5
    ) as ex:
        risultati = list(
            ex.map(_partite, leghe)
        )

    for league, partite in risultati:

        per_lega.append(
            (league, partite)
        )

        totali += len(partite)

    if not totali:

        print("❌ Nessuna partita trovata")

        return None

    compiti = []

    for league, partite in per_lega:

        for evento in partite:

            compiti.append(
                (league, evento, len(partite))
            )

    def _analizza(compito):

        league, evento, tot = compito

        try:
            return league, (
                analizza_partita(
                    evento,
                    league,
                    0,
                    tot
                )
            )

        except Exception as exc:

            print(
                f"❌ Errore analisi "
                f"partita: {exc}"
            )

            return league, None

    # fino a 4 analisi simultanee (storie squadra
    # e cache sono thread-safe)
    with ThreadPoolExecutor(
        max_workers=4
    ) as ex:
        risultati = list(
            ex.map(_analizza, compiti)
        )

    for league, analisi in risultati:

        fatti += 1

        nome = (
            CAMPIONATI.get(
                campionato_da_espn(
                    league
                ),
                config_competizione(
                    league
                ) or {}
            ).get(
                "nome",
                league
            )
        )

        if analisi:

            analisi["league"] = league

            # TUTTI i mercati (il taglio avviene qui
            # sotto, con l'unione top-prob + top-quota)
            picks_m = migliori_pick(
                analisi,
                max_pick=99
            )

            # unione top-8 per PROBABILITA' e top-8 per
            # QUOTA: le 42+ varianti avanzate hanno tante
            # gambe sicure che selezionandole solo per
            # probabilita' spazzerebbero via i mercati a
            # quota alta
            # (servono alle fasce AUDACE/MITO)
            # 3 gruppi per match: piu' PROBABILI, banda
            # CENTRALE (1.35-2.19) e piu' QUOTA: il pool
            # deve coprire TUTTA la scala quote, altrimenti
            # le fasce alte (AUDACE/MITO) diventano
            # irraggiungibili
            top_prob = picks_m[:6]

            mid = sorted(
                [
                    p for p in picks_m
                    if 1.35 <= p["quota"] <= 2.19
                ],
                key=lambda p: -p["prob"]
            )[:6]

            top_quota = sorted(
                picks_m,
                key=lambda p: -p["quota"]
            )[:6]

            visti_m = set()
            selezionati = []

            for p in top_prob + mid + top_quota:

                k = (
                    p["match_key"],
                    p["mercato"]
                )

                if k in visti_m:
                    continue

                visti_m.add(k)
                selezionati.append(p)

            pool.extend(selezionati)

        if on_progress:

            on_progress(
                fatti,
                totali,
                nome,
                (
                    analisi["home"]
                    if analisi
                    else "?"
                ),
                (
                    analisi["away"]
                    if analisi
                    else "?"
                )
            )

    print(
        f"🎟 Partite analizzate: "
        f"{len(pool)}/{totali}"
    )

    if len(pool) < 2:

        print(
            "❌ Pool insufficiente "
            "per le schedine"
        )

        return None

    picks_ordinate = sorted(
        pool,
        key=lambda p: (
            -p["aff"],
            -p["quota"],
            -p["prob"]
        )
    )

    schedine = []

    for tier in TIERS_SCHEDINE:

        try:

            s = costruisci_schedina(
                tier,
                picks_ordinate
            )

        except Exception as exc:

            print(
                f"❌ Errore costruzione "
                f"schedina {tier['nome']}: "
                f"{exc}"
            )

            s = None

        if s:

            schedine.append(s)

            print(
                f"   {tier['nome']}: "
                f"{len(s['legs'])} eventi, "
                f"quota {s['quota_tot']:.2f} "
                f"(cap {tier['cap']:.0f}), "
                f"aff {s['aff_media']}%"
            )

        else:

            print(
                f"   ⚠️ Nessuna combinazione "
                f"per {tier['nome']}"
            )

    if not schedine:
        return None

    # Se e' stato richiesto un cap specifico (click su un
    # singolo pulsante) restituisce SOLO quella schedina.
    if cap:

        selezionate = [
            s for s in schedine
            if abs(
                float(s["tier"]["cap"])
                - float(cap)
            ) < 0.5
        ]

        if not selezionate:

            disponibili = ", ".join(
                f"{s['tier']['cap']:.0f} "
                f"({s['quota_tot']:.2f})"
                for s in schedine
            )

            print("Nessuna schedina per cap " + str(cap))

            return [
                "Non sono riuscito a costruire una "
                "schedina entro quota "
                f"{cap:.0f}.\n\n"
                "Quelle riuscite: "
                + disponibili
            ]

        schedine = selezionate

    # notizie: avviso sulle gambe della schedina scelta
    for s in schedine:

        for leg in s["legs"]:

            nh = notizie_squadra(
                leg["analisi"]["home"]
            )

            na = notizie_squadra(
                leg["analisi"]["away"]
            )

            leg["news_flag"] = bool(
                (
                    nh
                    and nh.get("flag")
                )
                or (
                    na
                    and na.get("flag")
                )
            )

    print("Schedine create: " + str(len(schedine)))

    return [
        format_schedina(s)
        for s in schedine
    ]


# ============================================================
# MENU TELEGRAM
# ============================================================

def crea_menu_campionati():

    markup = types.InlineKeyboardMarkup(
        row_width=2
    )

    pulsanti = []

    for key, config in (
        CAMPIONATI.items()
    ):

        pulsanti.append(
            types.InlineKeyboardButton(
                key,
                callback_data=(
                    "campionato:"
                    + config["espn"]
                )
            )
        )

    markup.add(
        *pulsanti
    )

    markup.row(
        types.InlineKeyboardButton(
            "🌍 Nazionali",
            callback_data=(
                "sottomenu:naz"
            )
        ),
        types.InlineKeyboardButton(
            "🏆 CL & Europa",
            callback_data=(
                "sottomenu:europa"
            )
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "🎟 SICURA ≤10",
            callback_data="schedina:10"
        ),
        types.InlineKeyboardButton(
            "⚡ EQUILIBRATA ≤25",
            callback_data="schedina:25"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "🔥 AUDACE ≤50",
            callback_data="schedina:50"
        ),
        types.InlineKeyboardButton(
            "💣 MITO 51-60",
            callback_data="schedina:60"
        )
    )

    return markup


def campionato_da_espn(
    league: str
) -> Optional[str]:

    for key, config in (
        CAMPIONATI.items()
    ):

        if config["espn"] == league:
            return key

    return None


def crea_menu_sottomenu(
    cat_key: str
):

    """Tastiera del sottomenu: le sue competizioni
    (report singolo) + le 4 schedine sul pool di
    TUTTE le competizioni del sottomenu."""

    cat = SOTTOMENU.get(cat_key)

    if not cat:
        return None

    markup = types.InlineKeyboardMarkup(
        row_width=1
    )

    for c in cat["competizioni"]:

        markup.add(
            types.InlineKeyboardButton(
                c["btn"],
                callback_data=(
                    "campionato:"
                    + c["espn"]
                )
            )
        )

    scope = cat["scope"]

    markup.row(
        types.InlineKeyboardButton(
            "🎟 SICURA ≤10",
            callback_data=(
                f"schedina:10:{scope}"
            )
        ),
        types.InlineKeyboardButton(
            "⚡ EQUILIBRATA ≤25",
            callback_data=(
                f"schedina:25:{scope}"
            )
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "🔥 AUDACE ≤50",
            callback_data=(
                f"schedina:50:{scope}"
            )
        ),
        types.InlineKeyboardButton(
            "💣 MITO 51-60",
            callback_data=(
                f"schedina:60:{scope}"
            )
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "⬅️ Menu campionati",
            callback_data="menu:inizio"
        )
    )

    return markup


# ============================================================
# HANDLER /START E /HELP
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def comando_start(message):

    print(
        f"📩 /start ricevuto da chat "
        f"{message.chat.id}"
    )

    testo = """
<b>⚽ BOT PRONOSTICI CALCIO</b>

Benvenuto!

Seleziona il campionato da analizzare.

Il bot elaborerà:

• forma recente (ultime 10, tutte le competizioni)
• forma di solo campionato (ultime 8 in lega)
• rendimento casa/trasferta
• H2H ultime 5 (tutte le competizioni, 3 stagioni)
• impegni europei
• infortuni (football-data.org se configurata la chiave)
• riposo/fatica
• momentum statistico
• probabilità 1X2 (modello calibrato su dati storici)
• Over/Under 2.5
• Gol/No Gol
• goal attesi
• affidabilità dei dati
• 🎟 schedine pronte: SICURA (fino a 10), EQUILIBRATA (fino a 25), AUDACE (fino a 50), MITO (51-60)
• 📰 notizie reali di squadra (Google News) con avvisi infortuni/squalifiche\n• 🌍 Nazionali e 🏆 Champions/Europa: sottomenu con report e le stesse 4 schedine

<i>Le percentuali sono stime statistiche e non garantiscono il risultato.</i>
""".strip()

    bot.send_message(
        message.chat.id,
        testo,
        reply_markup=crea_menu_campionati()
    )


# ============================================================
# HANDLER TESTO
# ============================================================

ALIASES_CAMPIONATO = {
    "serie a": "ita.1",
    "seriea": "ita.1",
    "a": "ita.1",
    "italy": "ita.1",
    "italia": "ita.1",
    "premier league": "eng.1",
    "premier": "eng.1",
    "england": "eng.1",
    "ingleterra": "eng.1",
    "la liga": "esp.1",
    "liga": "esp.1",
    "spagna": "esp.1",
    "bundesliga": "ger.1",
    "germania": "ger.1",
    "germany": "ger.1",
    "ligue 1": "fra.1",
    "ligue1": "fra.1",
    "francia": "fra.1",
}


@bot.message_handler(
    func=lambda message: True
)
def messaggio_generico(message):

    raw = (
        message.text
        or message.caption
        or ""
    ).strip()

    # rimuove emoji e punteggi, minuscolo
    testo = re.sub(
        r"[^\w\s]",
        " ",
        raw,
        flags=re.UNICODE
    ).strip().lower()

    testo = re.sub(
        r"\s+",
        " ",
        testo
    )

    if testo in {
        "schedina",
        "schedine",
        "biglietto"
    }:

        avvia_schedina_chat(
            message.chat.id,
            None
        )

        return

    if testo in {
        "nazionali",
        "nazioni"
    }:

        try:
            bot.send_message(
                message.chat.id,
                "🌍 Nazionali",
                reply_markup=(
                    crea_menu_sottomenu("naz")
                )
            )
        except Exception:
            pass

        return

    if testo in {
        "champions",
        "champions league",
        "europa league",
        "coppe europa",
        "europa"
    }:

        try:
            bot.send_message(
                message.chat.id,
                "🏆 Champions & Europa",
                reply_markup=(
                    crea_menu_sottomenu(
                        "europa"
                    )
                )
            )
        except Exception:
            pass

        return

    league = ALIASES_CAMPIONATO.get(
        testo
    )

    if not league:

        bot.send_message(
            message.chat.id,
            "Usa /start per scegliere "
            "il campionato.",
            reply_markup=crea_menu_campionati()
        )

        return

    avvia_analisi_chat(
        message.chat.id,
        league
    )


# ============================================================
# CALLBACK CAMPIONATO
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
    (
        call.data
        and call.data.startswith(
            "campionato:"
        )
    )
)
def callback_campionato(call):

    print("CLICK CAMPIONATO ricevuto: " + str(getattr(call, "data", "?")))

    if not call.message:

        bot.answer_callback_query(
            call.id,
            "Premi /start per riavviare il menu."
        )

        return

    league = call.data.split(
        ":",
        1
    )[1]

    try:

        bot.answer_callback_query(
            call.id,
            "Analisi avviata..."
        )

    except Exception as exc:
        print(
            f"⚠️ Toast campionato: {exc}"
        )

    avvia_analisi_chat(
        call.message.chat.id,
        league
    )


@bot.callback_query_handler(
    func=lambda call:
    (
        call.data
        and call.data.startswith(
            "schedina:"
        )
    )
)
def callback_schedina(call):

    print("CLICK SCHEDINA ricevuto: " + str(getattr(call, "data", "?")))

    if not call.message:

        bot.answer_callback_query(
            call.id,
            "Premi /start per riavviare il menu."
        )

        return

    parti = call.data.split(":")

    try:
        cap = float(parti[1])
    except Exception:
        cap = 10.0

    scope = (
        parti[2]
        if len(parti) > 2
        else None
    )

    try:

        bot.answer_callback_query(
            call.id,
            "🎟 Preparo le schedine..."
        )

    except Exception as exc:
        print(
            f"⚠️ Toast schedina: {exc}"
        )

    avvia_schedina_chat(
        call.message.chat.id,
        cap,
        slugs=slug_per_scope(scope)
    )


@bot.callback_query_handler(
    func=lambda call:
    (
        call.data
        and call.data.startswith(
            "sottomenu:"
        )
    )
)
def callback_sottomenu(call):

    print("CLICK SOTTOMENU ricevuto: "
          + str(getattr(call, "data", "?")))

    if not call.message:

        bot.answer_callback_query(
            call.id,
            "Premi /start per riavviare il menu."
        )

        return

    cat_key = call.data.split(":", 1)[1]

    markup = crea_menu_sottomenu(cat_key)

    if not markup:

        bot.answer_callback_query(
            call.id,
            "Sottomenu non disponibile."
        )

        return

    try:
        bot.answer_callback_query(call.id)
    except Exception as exc:
        print(f"⚠️ Toast sottomenu: {exc}")

    titolo = SOTTOMENU[cat_key]["titolo"]

    try:
        bot.send_message(
            call.message.chat.id,
            (
                f"{titolo}\n\n"
                "Scegli la competizione da "
                "analizzare, oppure genera "
                "direttamente le 4 schedine "
                "(SICURA, EQUILIBRATA, AUDACE, "
                "MITO) su tutte le competizioni "
                "del sottomenu."
            ),
            reply_markup=markup
        )
    except Exception as exc:
        print(f"⚠️ Invio sottomenu: {exc}")


@bot.callback_query_handler(
    func=lambda call:
    (
        call.data
        and call.data.startswith(
            "menu:"
        )
    )
)
def callback_menu_inizio(call):

    if not call.message:

        bot.answer_callback_query(
            call.id,
            "Premi /start per riavviare il menu."
        )

        return

    try:
        bot.answer_callback_query(call.id)
    except Exception as exc:
        print(f"⚠️ Toast menu: {exc}")

    try:
        bot.send_message(
            call.message.chat.id,
            "⚽ <b>Menu principale</b>",
            reply_markup=crea_menu_campionati()
        )
    except Exception as exc:
        print(f"⚠️ Invio menu: {exc}")


# ============================================================
# AVVIO ANALISI
# ============================================================

_CHAT_LOCK = threading.Lock()
_CHAT_IN_ANALISI: set = set()
_SEMAFORO_ANALISI = (
    threading.Semaphore(
        ANALISI_MAX_SIMULTANEE
    )
)


def avvia_analisi_chat(
    chat_id: int,
    league: str
):

    nome_key = (
        campionato_da_espn(
            league
        )
    )

    config_extra = (
        None
        if nome_key
        else config_competizione(
            league
        )
    )

    if not nome_key and not config_extra:

        try:
            bot.send_message(
                chat_id,
                "❌ Campionato non riconosciuto."
            )
        except Exception as exc:
            print(
                f"❌ Errore risposta invio: {exc}"
            )

        return

    if nome_key:

        nome = CAMPIONATI[
            nome_key
        ]["nome"]

        chiave_report = nome_key

    else:

        nome = config_extra["nome"]

        chiave_report = league

    with _CHAT_LOCK:

        if chat_id in _CHAT_IN_ANALISI:

            try:
                bot.send_message(
                    chat_id,
                    (
                        "⏳ Un'analisi è già in "
                        "corso per questa chat.\n"
                        "Attendi il completamento "
                        "del report."
                    )
                )
            except Exception:
                pass

            return

        _CHAT_IN_ANALISI.add(
            chat_id
        )

    def lavoro():

        try:

            _SEMAFORO_ANALISI.acquire()

            try:

                print(
                    f"🚀 Avvio analisi Telegram: "
                    f"{nome}"
                )

                # messaggio di stato (editato in tempo reale)
                status_id = None

                try:
                    msg = bot.send_message(
                        chat_id,
                        (
                            f"⏳ <b>Analisi {nome} "
                            f"in corso...</b>\n\n"
                            "Sto recuperando le "
                            "partite e i dati "
                            "statistici da ESPN.\n"
                            "🔎 0 completate"
                        )
                    )
                    status_id = msg.message_id
                except Exception as exc:
                    print(
                        f"⚠️ Impossibile inviare "
                        f"messaggio iniziale: {exc}"
                    )

                def progresso(
                    indice: int,
                    totale: int,
                    home: str,
                    away: str
                ):

                    if not status_id:
                        return

                    try:
                        bot.edit_message_text(
                            (
                                f"⏳ <b>Analisi {nome} "
                                f"in corso...</b>\n\n"
                                f"✅ {indice}/{totale} "
                                f"partite analizzate\n"
                                f"🔎 Ultima: "
                                f"<b>{html_safe(home)} - "
                                f"{html_safe(away)}</b>"
                            ),
                            chat_id=chat_id,
                            message_id=status_id
                        )
                    except Exception as exc:
                        print(
                            f"⚠️ Errore edit "
                            f"progresso: {exc}"
                        )

                report = crea_report(
                    chiave_report,
                    on_progress=progresso
                )

                if not report:

                    if status_id:
                        try:
                            bot.edit_message_text(
                                (
                                    "❌ Nessun report "
                                    "disponibile."
                                ),
                                chat_id=chat_id,
                                message_id=status_id
                            )
                        except Exception:
                            pass

                    try:
                        bot.send_message(
                            chat_id,
                            "❌ Nessun report disponibile."
                        )
                    except Exception:
                        pass

                    return

                if status_id:
                    try:
                        bot.edit_message_text(
                            (
                                "✅ Analisi completata.\n"
                                "📨 Invio del report..."
                            ),
                            chat_id=chat_id,
                            message_id=status_id
                        )
                    except Exception:
                        pass

                invia_report(
                    chat_id,
                    report
                )

                print(
                    f"✅ ELABORAZIONE "
                    f"{nome} TERMINATA."
                )

            finally:

                _SEMAFORO_ANALISI.release()

        except Exception as exc:

            print(
                f"❌ ERRORE ELABORAZIONE "
                f"{nome}: {exc}"
            )

            try:
                bot.send_message(
                    chat_id,
                    (
                        "❌ Si è verificato "
                        "un errore durante "
                        "l'analisi.\n\n"
                        "Controlla i log di Render."
                    )
                )
            except Exception as exc2:

                print(
                    f"❌ Errore invio errore "
                    f"Telegram: {exc2}"
                )

        finally:

            with _CHAT_LOCK:
                _CHAT_IN_ANALISI.discard(
                    chat_id
                )

    threading.Thread(
        target=lavoro,
        daemon=True
    ).start()


def avvia_schedina_chat(
    chat_id: int,
    cap: float,
    slugs: Optional[List[str]] = None
):

    with _CHAT_LOCK:

        if chat_id in _CHAT_IN_ANALISI:

            try:
                bot.send_message(
                    chat_id,
                    (
                        "⏳ Un'analisi è già in "
                        "corso per questa chat.\n"
                        "Attendi il completamento."
                    )
                )
            except Exception:
                pass

            return

        _CHAT_IN_ANALISI.add(
            chat_id
        )

    def lavoro():

        try:

            _SEMAFORO_ANALISI.acquire()

            try:

                print(
                    f"🚀 Avvio schedine: cap {cap}"
                )

                ambito = (
                    "nel sottomenu"
                    if slugs
                    else "sui 5 campionati"
                )

                dove = (
                    "le competizioni del "
                    "sottomenu"
                    if slugs
                    else "tutti i 5 campionati"
                )

                status_id = None

                try:
                    msg = bot.send_message(
                        chat_id,
                        (
                            "🎟 <b>Preparazione "
                            "schedine in corso...</b>\n\n"
                            f"Analizzo {dove} per "
                            "scegliere le partite "
                            "più affidabili.\n"
                            "🔎 0/"
                        )
                    )
                    status_id = msg.message_id
                except Exception as exc:
                    print(
                        f"⚠️ Messaggio iniziale: {exc}"
                    )

                def progresso(
                    fatti: int,
                    totali: int,
                    lega: str,
                    home: str,
                    away: str
                ):

                    if not status_id:
                        return

                    try:
                        bot.edit_message_text(
                            (
                                "🎟 <b>Preparazione "
                                "schedine in corso...</b>\n\n"
                                f"✅ {fatti}/{totali} partite "
                                f"analizzate {ambito}\n"
                                f"🏟 {html_safe(lega)}: "
                                f"{html_safe(home)} - "
                                f"{html_safe(away)}"
                            ),
                            chat_id=chat_id,
                            message_id=status_id
                        )
                    except Exception as exc:
                        print(
                            f"⚠️ Edit progresso: {exc}"
                        )

                testi = crea_schedine(
                    cap,
                    on_progress=progresso,
                    slugs=slugs
                )

                if not testi:

                    testo_errore = (
                        "❌ Non sono riuscito a "
                        "creare le schedine.\n"
                        "Riprova più tardi."
                    )

                    if status_id:
                        try:
                            bot.edit_message_text(
                                testo_errore,
                                chat_id=chat_id,
                                message_id=status_id
                            )
                        except Exception:
                            pass

                    else:
                        try:
                            bot.send_message(
                                chat_id,
                                testo_errore
                            )
                        except Exception:
                            pass

                    return

                if status_id:
                    try:
                        bot.edit_message_text(
                            (
                                "✅ Analisi completate.\n"
                                "🎟 Invio della schedina..."
                            ),
                            chat_id=chat_id,
                            message_id=status_id
                        )
                    except Exception:
                        pass

                for testo in testi:

                    try:

                        bot.send_message(
                            chat_id,
                            testo,
                            disable_web_page_preview=True
                        )

                    except Exception as exc:

                        print(
                            f"❌ Invio schedina: {exc}"
                        )

                        try:

                            bot.send_message(
                                chat_id,
                                re.sub(
                                    r"<[^>]+>",
                                    "",
                                    testo
                                ),
                                parse_mode=None,
                                disable_web_page_preview=True
                            )

                        except Exception as exc2:

                            print(
                                f"❌ Fallback schedina: {exc2}"
                            )

                print(
                    "✅ SCHEDINE INVIATE."
                )

            finally:

                _SEMAFORO_ANALISI.release()

        except Exception as exc:

            print(
                f"❌ ERRORE SCHEDINE: {exc}"
            )

            try:
                bot.send_message(
                    chat_id,
                    (
                        "❌ Si è verificato un errore "
                        "durante la creazione delle "
                        "schedine.\nControlla i log."
                    )
                )
            except Exception:
                pass

        finally:

            with _CHAT_LOCK:
                _CHAT_IN_ANALISI.discard(
                    chat_id
                )

    threading.Thread(
        target=lavoro,
        daemon=True
    ).start()


# ============================================================
# WORKER AGGIORNAMENTI (coda + thread unico)
# ============================================================

def worker_aggiornamenti():

    print(
        "⚙️ Worker aggiornamenti avviato."
    )

    while True:

        body = (
            _UPDATE_QUEUE.get()
        )

        try:

            data = json.loads(
                body.decode(
                    "utf-8"
                )
            )

            update = (
                types.Update.de_json(
                    json.dumps(data)
                )
            )

            if update:

                bot.process_new_updates(
                    [update]
                )

                print(
                    "✅ Update Telegram "
                    "elaborato."
                )

            else:

                print(
                    "\u26a0\ufe0f Update non "
                    "valido (de_json None): "
                    + body[:200].decode(
                        "utf-8",
                        "ignore"
                    )
                )

        except Exception as exc:

            print(
                "❌ Errore elaborazione "
                f"update Telegram: {exc}"
            )


# ============================================================
# WEB SERVER RENDER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):
        return

    def do_GET(self):

        if self.path in {
            "/",
            "/health"
        }:

            risposta = {
                "status": "ok",
                "service": "bot-pronostici-gratis",
                "modalita": MODALITA,
                "webhook": WEBHOOK_URL,
                "tempo": int(
                    time.time()
                )
            }

            body = json.dumps(
                risposta,
                ensure_ascii=False
            ).encode("utf-8")

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json; "
                "charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(
                body
            )

            return

        self.send_response(
            404
        )

        self.end_headers()

    def do_POST(self):

        if self.path != WEBHOOK_PATH:

            self.send_response(
                404
            )

            self.end_headers()

            return

        try:

            # Autenticazione secret token
            if (
                TELEGRAM_SECRET_TOKEN
                and self.headers.get(
                    "X-Telegram-Bot-Api-Secret-Token"
                )
                != TELEGRAM_SECRET_TOKEN
            ):

                self.send_response(
                    403
                )

                self.end_headers()

                return

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            body = self.rfile.read(
                content_length
            )

            print(
                f"📩 POST RICEVUTA: "
                f"{self.path} "
                f"({len(body)} byte)"
            )

            # Risposta immediata a Telegram
            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.end_headers()

            self.wfile.write(
                b'{"ok":true}'
            )

            # Elaborazione in coda
            # (worker unico, sequenziale)
            _UPDATE_QUEUE.put(
                body
            )

        except Exception as exc:

            print(
                f"❌ Errore POST webhook: "
                f"{exc}"
            )

            try:
                self.send_response(
                    500
                )

                self.end_headers()
            except Exception:
                pass


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

def _webhook_raggiungibile() -> bool:

    try:

        response = requests.get(
            RENDER_EXTERNAL_URL
            + "/health",
            timeout=10
        )

        return (
            response.status_code == 200
        )

    except Exception as exc:

        print(
            f"\u26a0\ufe0f Self-check webhook: {exc}"
        )

        return False


_POLLING_ATTIVO = [False]


def _avvia_polling_fallback():

    if _POLLING_ATTIVO[0]:
        return

    _POLLING_ATTIVO[0] = True

    print(
        "\U0001f504 Fallback: avvio POLLING "
        "Telegram..."
    )

    def _loop():

        while True:

            try:
                bot.infinity_polling(
                    timeout=30,
                    long_polling_timeout=30,
                    skip_pending=False,
                    allowed_updates=ALLOWED_UPDATES
                )
            except Exception as exc:
                print(
                    f"\u274c Errore polling "
                    f"(riprovo in 10s): {exc}"
                )
                time.sleep(10)

    threading.Thread(
        target=_loop,
        daemon=True
    ).start()


def _verifica_webhook_background():

    """Verifica posticipata: il webhook e' gia' ATTIVO e
    gli update vengono elaborati dal worker; qui controlla
    solo che l'URL pubblico risponda. Se dopo 3 minuti non
    e' ancora raggiungibile (Render molto lento a bootare
    o egress bloccato) passa al polling.
    """

    for i in range(18):

        if _shutdown.is_set():
            return

        if _webhook_raggiungibile():

            print(
                "\u2705 Self-check OK "
                f"(al tentativo {i + 1}): "
                "webhook confermato."
            )

            return

        time.sleep(10)

    print(
        "\u26a0\ufe0f URL pubblico non raggiungibile "
        "dopo 3 minuti: passo a POLLING."
    )

    try:
        bot.remove_webhook()
    except Exception:
        pass

    global MODALITA

    MODALITA = "polling"

    _avvia_polling_fallback()


def configura_webhook() -> bool:

    global MODALITA

    print()
    print("=" * 50)

    print(
        "\U0001f310 CONFIGURAZIONE WEBHOOK TELEGRAM"
    )

    print(
        f"\U0001f310 URL webhook: "
        f"{WEBHOOK_URL}"
    )

    print(
        f"\U0001f310 Update richiesti: "
        f"{', '.join(ALLOWED_UPDATES)}"
    )

    print("=" * 50)

    impostato = False

    for tentativo in range(1, 4):

        print(
            f"\U0001f517 Tentativo {tentativo}/3..."
        )

        try:

            try:
                bot.remove_webhook()
                time.sleep(0.5)
            except Exception as exc:
                print(
                    f"\u2139\ufe0f remove_webhook: {exc}"
                )

            # drop_pending_updates=False: gli update
            # (click, comandi) arrivati mentre il
            # servizio era offline NON vengono
            # scartati ma consegnati al riavvio.
            risultato = bot.set_webhook(
                url=WEBHOOK_URL,
                drop_pending_updates=False,
                secret_token=TELEGRAM_SECRET_TOKEN,
                max_connections=40,
                allowed_updates=ALLOWED_UPDATES
            )

            if not risultato:
                raise RuntimeError(
                    "set_webhook ha restituito False"
                )

            impostato = True

            print(
                f"\u2705 Webhook configurato: "
                f"{risultato}"
            )

            break

        except Exception as exc:

            print(
                f"\u274c Errore configurazione "
                f"webhook: {exc}"
            )

            time.sleep(5)

    if not impostato:

        MODALITA = "polling"

        _avvia_polling_fallback()

        print(
            "\U0001f3c1 Bot operativo in modalita' "
            "POLLING (webhook non configurabile)."
        )

        return False

    # Webhook PRIMARIO: resta attivo fin da subito
    # (gli update in arrivo vengono comunque
    # elaborati dal worker). La raggiungibilita'
    # dell'URL pubblico si verifica in background:
    # durante il boot di Render il self-check fallisce
    # sempre, non deve degradare la modalita'.
    MODALITA = "webhook"

    threading.Thread(
        target=_verifica_webhook_background,
        daemon=True
    ).start()

    print(
        "\u2705 Webhook ATTIVO: verifiche di "
        "raggiungibilita' in background."
    )

    return True


# ============================================================
# MAIN
# ============================================================

_shutdown = threading.Event()


def _sigterm_handler(
    signum,
    frame
):

    print(
        "🛑 Segnale di arresto ricevuto."
    )

    _shutdown.set()


def main():

    print()
    print("=" * 50)

    print(
        "⚽ BOT PRONOSTICI CALCIO"
    )

    print(
        "\U0001f4a3 MITO 8 gambe + keep-alive - build 25 set 2026 v3"
    )

    print(
        "Avvio applicazione Render..."
    )

    print("=" * 50)

    try:
        signal.signal(
            signal.SIGTERM,
            _sigterm_handler
        )
    except Exception:
        pass

    # Worker aggiornamenti (coda)
    threading.Thread(
        target=worker_aggiornamenti,
        daemon=True
    ).start()

    # Server HTTP (thread dedicato)
    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    threading.Thread(
        target=server.serve_forever,
        daemon=True
    ).start()

    print(
        f"🌐 Server HTTP avviato "
        f"sulla porta {PORT}"
    )

    print(
        f"🌐 Health URL: "
        f"{RENDER_EXTERNAL_URL}/health"
    )

    # --------------------------------------------------------
    # KEEP-ALIVE: sul piano gratuito Render addormenta il
    # servizio dopo ~15 minuti di inattivita': il primo
    # messaggio Telegram va perso mentre il server si
    # sveglia (~50s). Un ping al proprio /health ogni
    # 10 minuti tiene il servizio sempre attivo
    # (disattivabile con la variabile KEEP_ALIVE=0).
    # --------------------------------------------------------

    if os.getenv("KEEP_ALIVE", "1").strip() != "0":

        def _keep_alive():

            while not _shutdown.is_set():

                try:

                    time.sleep(600)

                    requests.get(
                        RENDER_EXTERNAL_URL
                        + "/health",
                        timeout=10
                    )

                except Exception:
                    pass

        threading.Thread(
            target=_keep_alive,
            daemon=True
        ).start()

        print(
            "\U0001f493 Keep-alive attivo "
            "(ping ogni 10 min)"
        )

    # Webhook (con self-check e fallback)
    webhook_ok = (
        configura_webhook()
    )

    if not webhook_ok:

        print(
            "❌ ATTENZIONE: webhook Telegram "
            "non configurato, bot in "
            "modalità polling."
        )

    print()
    print("=" * 50)

    print(
        "✅ BOT TELEGRAM OPERATIVO"
    )

    print(
        f"✅ MODALITÀ: "
        f"{MODALITA.upper()}"
    )

    print(
        "✅ POLLING: "
        + (
            "ATTIVATO (fallback)"
            if MODALITA == "polling"
            else "DISATTIVATO"
        )
    )

    print("=" * 50)

    try:

        _shutdown.wait()

    except KeyboardInterrupt:

        print(
            "🛑 Arresto server..."
        )

    finally:

        try:
            bot.remove_webhook()
        except Exception:
            pass

        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

        print(
            "🛑 Server arrestato."
        )


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":
    main()
