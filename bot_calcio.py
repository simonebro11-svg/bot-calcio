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
            stats_home
        )
    )

    momentum_away = (
        calcola_momentum(
            stats_away
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
        stats_home,
        stats_away,
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
        stats_home,
        stats_away,
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
        stats_home,
        stats_away
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
        "meteo": wx,
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

{away}: {sa['sequenza']}
PPG {sa['ppg']:.2f} | GF {sa['gf']:.2f} | GS {sa['gs']:.2f}
PPG campionato: {lp_text(analisi['lp_away'])}

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

<b>⭐ PRONOSTICO PRINCIPALE</b>

<b>{migliore}</b> — {format_percent(migliore_valore)}

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

    if campionato_key not in CAMPIONATI:
        return None

    configurazione = CAMPIONATI[
        campionato_key
    ]

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

    picks = []

    for mercato, prob in ordine:

        prob = clamp(
            safe_float(prob, 1.0),
            1.0,
            99.0
        )

        picks.append({
            "analisi": analisi,
            "mercato": mercato,
            "prob": prob,
            "quota": quota_stimata(prob),
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

        righe.append(
            f"Pronostico: "
            f"<b>{html_safe(leg['mercato'])}</b> "
            f"({leg['prob']:.0f}%) "
            f"— quota {leg['quota']:.2f} "
            f"| affidabilità {leg['aff']}%"
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
    on_progress: Optional[Any] = None
) -> Optional[List[str]]:

    print()
    print("=" * 50)
    print("🎟 RICHIESTA SCHEDINE (cap: "
          + (str(cap) if cap else "tutte")
          + ")")
    print("=" * 50)

    pool = []

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
                {}
            ).get(
                "nome",
                league
            )
        )

        if analisi:

            analisi["league"] = league

            pool.extend(
                migliori_pick(
                    analisi,
                    max_pick=10
                )
            )

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

    try:
        cap = float(
            call.data.split(
                ":",
                1
            )[1]
        )
    except Exception:
        cap = 10.0

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
        cap
    )


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

    if not nome_key:

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

    nome = CAMPIONATI[
        nome_key
    ]["nome"]

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
                    nome_key,
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
    cap: float
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

                status_id = None

                try:
                    msg = bot.send_message(
                        chat_id,
                        (
                            "🎟 <b>Preparazione "
                            "schedine in corso...</b>\n\n"
                            "Analizzo tutti i 5 "
                            "campionati per scegliere "
                            "le partite più affidabili.\n"
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
                                "analizzate sui 5 campionati\n"
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
                    on_progress=progresso
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
            WEBHOOK_URL
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
