import os
import re
import json
import time
import math
import threading
import unicodedata
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

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

FOOTBALL_API_KEY = os.getenv(
    "FOOTBALL_API_KEY",
    ""
).strip()

HTTP_TIMEOUT = 20

CACHE_TTL = 30 * 60

GIORNI_FUTURI = 14

NUM_PARTITE_REPORT = 8

NUM_FORM = 10

PAGINE_EVENTI = 5

PAGINE_STORICO = 5


# ============================================================
# CONTROLLO CONFIGURAZIONE
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non configurato nelle Environment Variables."
    )

print("TELEGRAM_BOT_TOKEN: OK")

if FOOTBALL_API_KEY:
    print(
        "FOOTBALL_API_KEY: PRESENTE "
        "(non utilizzata: dati calcistici tramite SofaScore)"
    )
else:
    print(
        "⚠️ FOOTBALL_API_KEY non configurata "
        "(non necessaria per SofaScore)."
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)


# ============================================================
# SOFASCORE
# ============================================================

SOFASCORE_BASE = "https://www.sofascore.com/api/v1"

SOFASCORE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://www.sofascore.com/"
}


# ============================================================
# CAMPIONATI
# ============================================================

# ID ufficiali dei tornei Sofascore:
#
# Serie A       = 23
# Premier League = 17
# La Liga       = 8
# Bundesliga    = 35
# Ligue 1       = 34
#
# Gli ID sono quelli dei tornei Sofascore.
#
# La stagione viene recuperata automaticamente.

CAMPIONATI = {

    "🇮🇹 Serie A": {
        "id": 23,
        "nome": "Serie A"
    },

    "🏴 Premier League": {
        "id": 17,
        "nome": "Premier League"
    },

    "🇪🇸 La Liga": {
        "id": 8,
        "nome": "La Liga"
    },

    "🇩🇪 Bundesliga": {
        "id": 35,
        "nome": "Bundesliga"
    },

    "🇫🇷 Ligue 1": {
        "id": 34,
        "nome": "Ligue 1"
    }
}


# ============================================================
# COMPETIZIONI EUROPEE
# ============================================================

COMPETIZIONI_EUROPEE = {

    "champions": "Champions League",
    "europa league": "Europa League",
    "conference": "Conference League"

}


# ============================================================
# CACHE
# ============================================================

_CACHE: Dict[str, Tuple[float, Any]] = {}

_CACHE_LOCK = threading.Lock()


def cache_get(key: str):

    with _CACHE_LOCK:

        item = _CACHE.get(key)

        if not item:
            return None

        timestamp, value = item

        if time.time() - timestamp > CACHE_TTL:

            del _CACHE[key]

            return None

        return value


def cache_set(
    key: str,
    value: Any
):

    with _CACHE_LOCK:

        _CACHE[key] = (
            time.time(),
            value
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
        c
        for c in testo
        if not unicodedata.combining(c)
    )

    testo = testo.lower()

    sostituzioni = {

        "internazionale":
            "inter",

        "internazionale milano":
            "inter",

        "inter milan":
            "inter",

        "internazionale fc":
            "inter",

        "inter fc":
            "inter",

        "ac milan":
            "milan",

        "milan ac":
            "milan",

        "ac milan":
            "milan",

        "ss lazio":
            "lazio",

        "ss lazio roma":
            "lazio",

        "as roma":
            "roma",

        "as roma calcio":
            "roma",

        "juventus fc":
            "juventus",

        "fc juventus":
            "juventus",

        "paris saint-germain":
            "psg",

        "paris saint germain":
            "psg",

        "psg":
            "psg",

        "manchester united fc":
            "manchester united",

        "manchester city fc":
            "manchester city",

        "tottenham hotspur":
            "tottenham",

        "tottenham hotspur fc":
            "tottenham",

        "inter de milan":
            "inter",

        "atletico madrid":
            "atletico madrid",

        "atletico de madrid":
            "atletico madrid"
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

    return (
        f"{clamp(value, 0, 100):.0f}%"
    )


def parse_timestamp(
    timestamp
) -> Optional[datetime]:

    try:

        if timestamp is None:
            return None

        dt = datetime.fromtimestamp(
            int(timestamp),
            tz=timezone.utc
        )

        return dt

    except Exception:

        return None


# ============================================================
# HTTP SOFASCORE
# ============================================================

def sofascore_get(
    path: str,
    cache_key: Optional[str] = None,
    use_cache: bool = True
):

    if cache_key and use_cache:

        cached = cache_get(
            cache_key
        )

        if cached is not None:
            return cached

    url = (
        SOFASCORE_BASE
        + "/"
        + path.lstrip("/")
    )

    try:

        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers=SOFASCORE_HEADERS
        )

        if response.status_code != 200:

            print(
                f"⚠️ SOFASCORE HTTP "
                f"{response.status_code}: "
                f"{url}"
            )

            return None

        data = response.json()

        if cache_key and use_cache:

            cache_set(
                cache_key,
                data
            )

        return data

    except requests.RequestException as exc:

        print(
            f"❌ Errore richiesta SofaScore: "
            f"{exc}"
        )

        return None

    except ValueError as exc:

        print(
            f"❌ JSON SofaScore non valido: "
            f"{exc}"
        )

        return None

    except Exception as exc:

        print(
            f"❌ Errore SofaScore: "
            f"{exc}"
        )

        return None


# ============================================================
# STAGIONE CORRENTE
# ============================================================

def recupera_stagione_corrente(
    tournament_id: int
) -> Optional[Dict[str, Any]]:

    cache_key = (
        f"season_{tournament_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    data = sofascore_get(
        f"unique-tournament/{tournament_id}/seasons",
        cache_key=cache_key
    )

    if not data:
        return None

    seasons = data.get(
        "seasons",
        []
    )

    if not isinstance(
        seasons,
        list
    ):
        return None

    if not seasons:
        return None

    # Prima cerchiamo la stagione corrente
    # confrontando l'anno corrente.

    anno = datetime.now(
        timezone.utc
    ).year

    candidati = []

    for season in seasons:

        if not isinstance(
            season,
            dict
        ):
            continue

        season_name = str(
            season.get(
                "name",
                ""
            )
        )

        season_id = season.get(
            "id"
        )

        if season_id is None:
            continue

        candidati.append(
            season
        )

        if str(anno) in season_name:

            cache_set(
                cache_key,
                season
            )

            return season

    # Normalmente la prima stagione
    # restituita è la più recente.

    stagione = candidati[0]

    cache_set(
        cache_key,
        stagione
    )

    return stagione


# ============================================================
# EVENTI FUTURI TORNEO
# ============================================================

def recupera_partite_future(
    campionato_key: str
) -> List[Dict[str, Any]]:

    configurazione = CAMPIONATI[
        campionato_key
    ]

    tournament_id = configurazione[
        "id"
    ]

    nome = configurazione[
        "nome"
    ]

    print(
        f"📅 Recupero partite future: "
        f"{nome}"
    )

    cache_key = (
        f"future_{tournament_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    stagione = recupera_stagione_corrente(
        tournament_id
    )

    if not stagione:

        print(
            "❌ Stagione SofaScore "
            "non disponibile."
        )

        return []

    season_id = stagione.get(
        "id"
    )

    if not season_id:
        return []

    oggi = datetime.now(
        timezone.utc
    )

    limite = oggi + timedelta(
        days=GIORNI_FUTURI
    )

    eventi = []

    ids_visti = set()

    for pagina in range(
        PAGINE_EVENTI
    ):

        data = sofascore_get(
            (
                f"unique-tournament/"
                f"{tournament_id}/"
                f"season/{season_id}/"
                f"events/next/{pagina}"
            ),
            cache_key=(
                f"next_{tournament_id}_"
                f"{season_id}_{pagina}"
            )
        )

        if not data:
            continue

        lista = data.get(
            "events",
            []
        )

        if not isinstance(
            lista,
            list
        ):
            continue

        for evento in lista:

            if not isinstance(
                evento,
                dict
            ):
                continue

            event_id = str(
                evento.get(
                    "id",
                    ""
                )
            )

            if not event_id:
                continue

            if event_id in ids_visti:
                continue

            ids_visti.add(
                event_id
            )

            timestamp = evento.get(
                "startTimestamp"
            )

            dt = parse_timestamp(
                timestamp
            )

            if not dt:
                continue

            if dt <= oggi:
                continue

            if dt > limite:
                continue

            home = evento.get(
                "homeTeam"
            )

            away = evento.get(
                "awayTeam"
            )

            if not home or not away:
                continue

            evento["_datetime"] = dt

            eventi.append(
                evento
            )

    eventi.sort(
        key=lambda x:
        x.get(
            "_datetime",
            datetime.max.replace(
                tzinfo=timezone.utc
            )
        )
    )

    risultati = eventi[
        :NUM_PARTITE_REPORT
    ]

    cache_set(
        cache_key,
        risultati
    )

    print(
        f"📅 Partite future trovate: "
        f"{len(risultati)}"
    )

    return risultati


# ============================================================
# DATI SQUADRA
# ============================================================

def nome_team(
    team: Dict[str, Any]
) -> str:

    if not team:
        return "Sconosciuta"

    return (
        team.get("name")
        or team.get("shortName")
        or team.get("slug")
        or "Sconosciuta"
    )


def id_team(
    team: Dict[str, Any]
) -> Optional[str]:

    if not team:
        return None

    value = team.get(
        "id"
    )

    if value is None:
        return None

    return str(value)


def evento_team(
    evento: Dict[str, Any],
    team_id: str
) -> bool:

    home = evento.get(
        "homeTeam",
        {}
    )

    away = evento.get(
        "awayTeam",
        {}
    )

    return (
        str(home.get("id"))
        == str(team_id)
        or
        str(away.get("id"))
        == str(team_id)
    )


def evento_terminato(
    evento: Dict[str, Any]
) -> bool:

    status = evento.get(
        "status",
        {}
    )

    tipo = str(
        status.get(
            "type",
            ""
        )
    ).lower()

    codice = str(
        status.get(
            "code",
            ""
        )
    ).lower()

    return (
        tipo in {
            "finished",
            "ended",
            "afterpenalties",
            "afterextratime"
        }
        or codice in {
            "100",
            "110",
            "120"
        }
    )


def score_team(
    evento: Dict[str, Any],
    home: bool
) -> Optional[int]:

    score_key = (
        "homeScore"
        if home
        else "awayScore"
    )

    score = evento.get(
        score_key
    )

    if not isinstance(
        score,
        dict
    ):
        return None

    for key in (
        "current",
        "normaltime",
        "normaltime"
    ):

        if score.get(key) is not None:

            try:
                return int(
                    score.get(key)
                )
            except Exception:
                pass

    return None


# ============================================================
# EVENTI STORICI SQUADRA
# ============================================================

def recupera_eventi_storici_squadra(
    team_id: Optional[str]
) -> List[Dict[str, Any]]:

    if not team_id:
        return []

    cache_key = (
        f"team_last_{team_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    eventi = []

    ids_visti = set()

    for pagina in range(
        PAGINE_STORICO
    ):

        data = sofascore_get(
            (
                f"team/{team_id}/"
                f"events/last/{pagina}"
            ),
            cache_key=(
                f"team_last_"
                f"{team_id}_"
                f"{pagina}"
            )
        )

        if not data:
            continue

        lista = data.get(
            "events",
            []
        )

        if not isinstance(
            lista,
            list
        ):
            continue

        for evento in lista:

            if not isinstance(
                evento,
                dict
            ):
                continue

            event_id = str(
                evento.get(
                    "id",
                    ""
                )
            )

            if not event_id:
                continue

            if event_id in ids_visti:
                continue

            ids_visti.add(
                event_id
            )

            if not evento_team(
                evento,
                team_id
            ):
                continue

            if not evento_terminato(
                evento
            ):
                continue

            dt = parse_timestamp(
                evento.get(
                    "startTimestamp"
                )
            )

            if not dt:
                continue

            evento["_datetime"] = dt

            eventi.append(
                evento
            )

    eventi.sort(
        key=lambda x:
        x.get(
            "_datetime",
            datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True
    )

    cache_set(
        cache_key,
        eventi
    )

    return eventi


# ============================================================
# FORMA
# ============================================================

def recupera_form(
    team_id: Optional[str],
    tournament_id: int
) -> List[Dict[str, Any]]:

    if not team_id:
        return []

    cache_key = (
        f"form_{team_id}_"
        f"{tournament_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    eventi = recupera_eventi_storici_squadra(
        team_id
    )

    partite = []

    for evento in eventi:

        tournament = evento.get(
            "tournament",
            {}
        )

        tournament_obj = tournament.get(
            "uniqueTournament",
            {}
        )

        evento_tournament_id = (
            tournament_obj.get("id")
        )

        if evento_tournament_id is None:

            evento_tournament_id = (
                tournament.get("id")
            )

        if safe_int(
            evento_tournament_id,
            -1
        ) != tournament_id:

            continue

        home_team = evento.get(
            "homeTeam",
            {}
        )

        away_team = evento.get(
            "awayTeam",
            {}
        )

        home_score = score_team(
            evento,
            True
        )

        away_score = score_team(
            evento,
            False
        )

        if (
            home_score is None
            or
            away_score is None
        ):
            continue

        partite.append(
            {
                "id": str(
                    evento.get(
                        "id"
                    )
                ),

                "date": evento.get(
                    "_datetime"
                ),

                "home": nome_team(
                    home_team
                ),

                "away": nome_team(
                    away_team
                ),

                "home_id": id_team(
                    home_team
                ),

                "away_id": id_team(
                    away_team
                ),

                "home_score":
                    home_score,

                "away_score":
                    away_score,

                "tournament":
                    tournament.get(
                        "name",
                        ""
                    )
            }
        )

        if len(partite) >= NUM_FORM:
            break

    cache_set(
        cache_key,
        partite
    )

    return partite


# ============================================================
# STATISTICHE FORMA
# ============================================================

def risultato_team(
    match: Dict[str, Any],
    team_id: str
) -> str:

    home_id = str(
        match.get(
            "home_id"
        )
    )

    away_id = str(
        match.get(
            "away_id"
        )
    )

    hg = safe_int(
        match.get(
            "home_score"
        )
    )

    ag = safe_int(
        match.get(
            "away_score"
        )
    )

    if home_id == str(
        team_id
    ):

        if hg > ag:
            return "V"

        if hg < ag:
            return "S"

        return "P"

    if away_id == str(
        team_id
    ):

        if ag > hg:
            return "V"

        if ag < hg:
            return "S"

        return "P"

    return "?"


def statistiche_form(
    form: List[Dict[str, Any]],
    team_id: str
) -> Dict[str, Any]:

    risultati = []

    punti = 0

    gol_fatti = 0

    gol_subiti = 0

    for match in form:

        risultato = risultato_team(
            match,
            team_id
        )

        if risultato == "?":
            continue

        risultati.append(
            risultato
        )

        home_id = str(
            match.get(
                "home_id"
            )
        )

        if home_id == str(
            team_id
        ):

            gf = safe_int(
                match.get(
                    "home_score"
                )
            )

            gs = safe_int(
                match.get(
                    "away_score"
                )
            )

        else:

            gf = safe_int(
                match.get(
                    "away_score"
                )
            )

            gs = safe_int(
                match.get(
                    "home_score"
                )
            )

        gol_fatti += gf

        gol_subiti += gs

        if risultato == "V":
            punti += 3

        elif risultato == "P":
            punti += 1

    numero = len(
        risultati
    )

    return {

        "sequenza":
            "".join(risultati)
            or "N/D",

        "ppg":
            punti / numero
            if numero
            else 0,

        "gf":
            gol_fatti / numero
            if numero
            else 0,

        "gs":
            gol_subiti / numero
            if numero
            else 0,

        "gol_fatti":
            gol_fatti,

        "gol_subiti":
            gol_subiti,

        "partite":
            numero
    }


# ============================================================
# CASA / TRASFERTA
# ============================================================

def rendimento_casa(
    form: List[Dict[str, Any]],
    team_id: str
) -> Optional[float]:

    valori = []

    for match in form:

        if str(
            match.get(
                "home_id"
            )
        ) != str(team_id):

            continue

        hg = safe_int(
            match.get(
                "home_score"
            )
        )

        ag = safe_int(
            match.get(
                "away_score"
            )
        )

        if hg > ag:
            valori.append(3)

        elif hg == ag:
            valori.append(1)

        else:
            valori.append(0)

    if not valori:
        return None

    return sum(
        valori
    ) / len(
        valori
    )


def rendimento_trasferta(
    form: List[Dict[str, Any]],
    team_id: str
) -> Optional[float]:

    valori = []

    for match in form:

        if str(
            match.get(
                "away_id"
            )
        ) != str(team_id):

            continue

        hg = safe_int(
            match.get(
                "home_score"
            )
        )

        ag = safe_int(
            match.get(
                "away_score"
            )
        )

        if ag > hg:
            valori.append(3)

        elif ag == hg:
            valori.append(1)

        else:
            valori.append(0)

    if not valori:
        return None

    return sum(
        valori
    ) / len(
        valori
    )


# ============================================================
# CLASSIFICA
# ============================================================

def recupera_classifica(
    tournament_id: int
) -> Optional[List[Dict[str, Any]]]:

    stagione = recupera_stagione_corrente(
        tournament_id
    )

    if not stagione:
        return None

    season_id = stagione.get(
        "id"
    )

    if not season_id:
        return None

    cache_key = (
        f"standings_"
        f"{tournament_id}_"
        f"{season_id}"
    )

    data = sofascore_get(
        (
            f"unique-tournament/"
            f"{tournament_id}/"
            f"season/{season_id}/"
            f"standings/total"
        ),
        cache_key=cache_key
    )

    if not data:
        return None

    standings = data.get(
        "standings",
        []
    )

    if not isinstance(
        standings,
        list
    ):
        return None

    for tabella in standings:

        if isinstance(
            tabella,
            dict
        ):

            rows = tabella.get(
                "rows"
            )

            if isinstance(
                rows,
                list
            ):

                return rows

    return None


def trova_classifica_team(
    rows: Optional[List[Dict[str, Any]]],
    team_id: Optional[str],
    team_name: str
) -> Optional[Dict[str, Any]]:

    if not rows:
        return None

    team_norm = normalizza_nome(
        team_name
    )

    # Primo tentativo: ID
    if team_id:

        for row in rows:

            team = row.get(
                "team",
                {}
            )

            if str(
                team.get("id")
            ) == str(team_id):

                return row

    # Secondo tentativo: nome
    for row in rows:

        team = row.get(
            "team",
            {}
        )

        nome = nome_team(
            team
        )

        nome_norm = normalizza_nome(
            nome
        )

        if nome_norm == team_norm:
            return row

        if (
            team_norm in nome_norm
            or
            nome_norm in team_norm
        ):
            return row

    return None


def format_classifica(
    row: Optional[Dict[str, Any]]
) -> str:

    if not row:
        return "N/D"

    posizione = row.get(
        "position"
    )

    punti = row.get(
        "points"
    )

    partite = row.get(
        "matches"
    )

    if posizione is None:
        posizione = "?"

    if punti is None:
        punti = "?"

    if partite is None:

        return (
            f"{posizione}° posto, "
            f"{punti} punti"
        )

    return (
        f"{posizione}° posto, "
        f"{punti} punti "
        f"({partite} gare)"
    )


# ============================================================
# H2H
# ============================================================

def recupera_h2h(
    home_id: Optional[str],
    away_id: Optional[str]
) -> List[Dict[str, Any]]:

    if not home_id or not away_id:
        return []

    cache_key = (
        f"h2h_"
        f"{home_id}_"
        f"{away_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    eventi = recupera_eventi_storici_squadra(
        home_id
    )

    risultati = []

    for evento in eventi:

        home_team = evento.get(
            "homeTeam",
            {}
        )

        away_team = evento.get(
            "awayTeam",
            {}
        )

        if not (
            (
                str(
                    home_team.get("id")
                ) == str(home_id)
                and
                str(
                    away_team.get("id")
                ) == str(away_id)
            )
            or
            (
                str(
                    home_team.get("id")
                ) == str(away_id)
                and
                str(
                    away_team.get("id")
                ) == str(home_id)
            )
        ):

            continue

        hs = score_team(
            evento,
            True
        )

        ass = score_team(
            evento,
            False
        )

        if (
            hs is None
            or
            ass is None
        ):
            continue

        risultati.append(
            {
                "date":
                    evento.get(
                        "_datetime"
                    ),

                "home":
                    nome_team(
                        home_team
                    ),

                "away":
                    nome_team(
                        away_team
                    ),

                "home_id":
                    id_team(
                        home_team
                    ),

                "away_id":
                    id_team(
                        away_team
                    ),

                "home_score":
                    hs,

                "away_score":
                    ass
            }
        )

        if len(risultati) >= 5:
            break

    risultati.sort(
        key=lambda x:
        x.get(
            "date",
            datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True
    )

    risultati = risultati[:5]

    cache_set(
        cache_key,
        risultati
    )

    return risultati


# ============================================================
# RIPOSO / FATICA
# ============================================================

def giorni_dall_ultima_partita(
    eventi: List[Dict[str, Any]]
) -> Optional[float]:

    if not eventi:
        return None

    date = []

    for evento in eventi:

        dt = evento.get(
            "_datetime"
        )

        if dt:
            date.append(
                dt
            )

    if not date:
        return None

    ultima = max(
        date
    )

    oggi = datetime.now(
        timezone.utc
    )

    delta = (
        oggi - ultima
    )

    return max(
        0,
        delta.total_seconds()
        / 86400
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
# EUROPA
# ============================================================

def identifica_europa(
    evento: Dict[str, Any]
) -> Optional[str]:

    tournament = evento.get(
        "tournament",
        {}
    )

    nome = str(
        tournament.get(
            "name",
            ""
        )
    ).lower()

    unique = tournament.get(
        "uniqueTournament",
        {}
    )

    nome_unique = str(
        unique.get(
            "name",
            ""
        )
    ).lower()

    testo = (
        nome
        + " "
        + nome_unique
    )

    if (
        "champions league"
        in testo
    ):
        return "Champions League"

    if (
        "europa league"
        in testo
    ):
        return "Europa League"

    if (
        "conference league"
        in testo
    ):
        return "Conference League"

    return None


def verifica_impegni_europei(
    team_id: Optional[str]
) -> Dict[str, Any]:

    if not team_id:

        return {
            "stato":
                "non_disponibile",

            "competizioni":
                [],

            "ultima":
                None
        }

    eventi = recupera_eventi_storici_squadra(
        team_id
    )

    if not eventi:

        return {
            "stato":
                "non_disponibile",

            "competizioni":
                [],

            "ultima":
                None
        }

    limite = (
        datetime.now(
            timezone.utc
        )
        - timedelta(
            days=45
        )
    )

    europee = []

    for evento in eventi:

        dt = evento.get(
            "_datetime"
        )

        if not dt:
            continue

        if dt < limite:
            continue

        competizione = identifica_europa(
            evento
        )

        if competizione:

            europee.append(
                {
                    "date":
                        dt,

                    "competizione":
                        competizione,

                    "evento":
                        evento
                }
            )

    europee.sort(
        key=lambda x:
        x["date"],
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

    return {

        "stato":
            "ok",

        "competizioni":
            competizioni,

        "ultima":
            (
                europee[0]
                if europee
                else None
            )
    }


def format_europa(
    europa: Dict[str, Any]
) -> str:

    if not europa:
        return "⚠️ dati non disponibili"

    if europa.get(
        "stato"
    ) != "ok":

        return (
            "⚠️ dati non disponibili"
        )

    competizioni = europa.get(
        "competizioni",
        []
    )

    if not competizioni:

        return (
            "Nessun impegno europeo "
            "recente rilevato"
        )

    return ", ".join(
        competizioni
    )


# ============================================================
# INFORTUNI
# ============================================================

def recupera_infortuni(
    team_id: Optional[str]
) -> Optional[List[Dict[str, Any]]]:

    # SofaScore non viene utilizzato qui
    # per inventare un elenco infortuni.
    #
    # Se non disponiamo di una fonte affidabile
    # specifica, restituiamo None.

    return None


def format_infortuni(
    infortuni: Optional[List[Dict[str, Any]]]
) -> str:

    if infortuni is None:

        return (
            "⚠️ dati infortuni "
            "non disponibili"
        )

    if not infortuni:

        return (
            "✅ nessuna segnalazione"
        )

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

        righe.append(
            f"• {nome} — {stato}"
        )

    return "\n".join(
        righe
    )


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

    differenza = (
        gf - gs
    )

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
# PROBABILITÀ 1X2
# ============================================================

def calcola_probabilita(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float],
    fatigue_home: int,
    fatigue_away: int,
    h2h: List[Dict[str, Any]],
    class_home: Optional[Dict[str, Any]],
    class_away: Optional[Dict[str, Any]]
) -> Tuple[float, float, float]:

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

    differenza_forma = (
        home_ppg
        - away_ppg
    )

    home += (
        differenza_forma
        * 7
    )

    away -= (
        differenza_forma
        * 7
    )

    if ppg_casa is not None:

        home += (
            ppg_casa - 1.5
        ) * 8

    if ppg_trasferta is not None:

        away += (
            ppg_trasferta - 1.5
        ) * 8

    # Vantaggio campo
    home += 5

    # Fatica
    home -= (
        fatigue_home
        * 0.08
    )

    away -= (
        fatigue_away
        * 0.08
    )

    # Momentum
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

    differenza_momentum = (
        momentum_home
        - momentum_away
    )

    home += (
        differenza_momentum
        * 0.35
    )

    away -= (
        differenza_momentum
        * 0.35
    )

    # Classifica
    if (
        class_home
        and
        class_away
    ):

        ph = safe_float(
            class_home.get(
                "points"
            )
        )

        pa = safe_float(
            class_away.get(
                "points"
            )
        )

        if ph or pa:

            diff = ph - pa

            bonus = clamp(
                diff * 0.12,
                -6,
                6
            )

            home += bonus

            away -= bonus

    # H2H
    h2h_home = 0
    h2h_draw = 0
    h2h_away = 0

    for match in h2h:

        mh = str(
            match.get(
                "home_id"
            )
        )

        ma = str(
            match.get(
                "away_id"
            )
        )

        hg = safe_int(
            match.get(
                "home_score"
            )
        )

        ag = safe_int(
            match.get(
                "away_score"
            )
        )

        if mh == str(
            class_home.get(
                "team",
                {}
            ).get(
                "id"
            )
        ) if class_home else False:

            gol_home = hg
            gol_away = ag

        elif ma == str(
            class_home.get(
                "team",
                {}
            ).get(
                "id"
            )
        ) if class_home else False:

            gol_home = ag
            gol_away = hg

        else:

            # Fallback: confronto tramite
            # ordine della partita H2H.
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

    # Equilibrio
    if abs(
        home - away
    ) < 5:

        draw += 4

    elif abs(
        home - away
    ) < 10:

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

    return (

        home
        / totale
        * 100,

        draw
        / totale
        * 100,

        away
        / totale
        * 100
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


def probabilita_over_25(
    expected_goals: float
) -> float:

    under = 0.0

    for k in range(3):

        under += poisson_prob(
            expected_goals,
            k
        )

    return clamp(
        (1 - under) * 100,
        0,
        100
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

    return clamp(
        indice,
        10,
        90
    )


# ============================================================
# AFFIDABILITÀ
# ============================================================

def calcola_affidabilita(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    class_home: Optional[Dict[str, Any]],
    class_away: Optional[Dict[str, Any]],
    injuries_home: Optional[List],
    injuries_away: Optional[List],
    h2h: List,
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float]
) -> int:

    score = 45

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

    if class_home:
        score += 5

    if class_away:
        score += 5

    if ppg_casa is not None:
        score += 3

    if ppg_trasferta is not None:
        score += 3

    if len(
        h2h
    ) >= 3:

        score += 3

    # Non premiamo i dati infortuni
    # se non sono realmente disponibili.

    return int(
        clamp(
            score,
            25,
            85
        )
    )


# ============================================================
# ANALISI PARTITA
# ============================================================

def analizza_partita(
    evento: Dict[str, Any],
    campionato_key: str,
    indice: int,
    totale: int
) -> Optional[Dict[str, Any]]:

    config = CAMPIONATI[
        campionato_key
    ]

    tournament_id = config[
        "id"
    ]

    home_team = evento.get(
        "homeTeam",
        {}
    )

    away_team = evento.get(
        "awayTeam",
        {}
    )

    home_name = nome_team(
        home_team
    )

    away_name = nome_team(
        away_team
    )

    home_id = id_team(
        home_team
    )

    away_id = id_team(
        away_team
    )

    if not home_id or not away_id:
        return None

    print(
        f"🔎 Analisi {indice}/{totale}: "
        f"{home_name} - {away_name}"
    )

    # --------------------------------------------------------
    # FORMA
    # --------------------------------------------------------

    form_home = recupera_form(
        home_id,
        tournament_id
    )

    form_away = recupera_form(
        away_id,
        tournament_id
    )

    stats_home = statistiche_form(
        form_home,
        home_id
    )

    stats_away = statistiche_form(
        form_away,
        away_id
    )

    # --------------------------------------------------------
    # CASA / TRASFERTA
    # --------------------------------------------------------

    ppg_casa = rendimento_casa(
        form_home,
        home_id
    )

    ppg_trasferta = rendimento_trasferta(
        form_away,
        away_id
    )

    # --------------------------------------------------------
    # CLASSIFICA
    # --------------------------------------------------------

    standings = recupera_classifica(
        tournament_id
    )

    class_home = trova_classifica_team(
        standings,
        home_id,
        home_name
    )

    class_away = trova_classifica_team(
        standings,
        away_id,
        away_name
    )

    # --------------------------------------------------------
    # INFORTUNI
    # --------------------------------------------------------

    injuries_home = recupera_infortuni(
        home_id
    )

    injuries_away = recupera_infortuni(
        away_id
    )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h = recupera_h2h(
        home_id,
        away_id
    )

    # --------------------------------------------------------
    # EUROPA
    # --------------------------------------------------------

    europe_home = verifica_impegni_europei(
        home_id
    )

    europe_away = verifica_impegni_europei(
        away_id
    )

    # --------------------------------------------------------
    # RIPOSO
    # --------------------------------------------------------

    eventi_home = recupera_eventi_storici_squadra(
        home_id
    )

    eventi_away = recupera_eventi_storici_squadra(
        away_id
    )

    riposo_home = giorni_dall_ultima_partita(
        eventi_home
    )

    riposo_away = giorni_dall_ultima_partita(
        eventi_away
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

    momentum_home = calcola_momentum(
        stats_home
    )

    momentum_away = calcola_momentum(
        stats_away
    )

    # --------------------------------------------------------
    # PROBABILITÀ
    # --------------------------------------------------------

    prob_home, prob_draw, prob_away = (
        calcola_probabilita(
            stats_home,
            stats_away,
            ppg_casa,
            ppg_trasferta,
            fatica_home,
            fatica_away,
            h2h,
            class_home,
            class_away
        )
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

    over25 = probabilita_over_25(
        expected_goals
    )

    under25 = (
        100
        - over25
    )

    gol = probabilita_gol(
        stats_home,
        stats_away
    )

    no_gol = (
        100
        - gol
    )

    # --------------------------------------------------------
    # AFFIDABILITÀ
    # --------------------------------------------------------

    affidabilita = calcola_affidabilita(
        stats_home,
        stats_away,
        class_home,
        class_away,
        injuries_home,
        injuries_away,
        h2h,
        ppg_casa,
        ppg_trasferta
    )

    # --------------------------------------------------------
    # MERCATI
    # --------------------------------------------------------

    mercati = {

        "1":
            prob_home,

        "X":
            prob_draw,

        "2":
            prob_away,

        "Over 2.5":
            over25,

        "Under 2.5":
            under25,

        "Gol":
            gol,

        "No Gol":
            no_gol
    }

    migliore = max(
        mercati,
        key=mercati.get
    )

    return {

        "evento":
            evento,

        "home":
            home_name,

        "away":
            away_name,

        "home_id":
            home_id,

        "away_id":
            away_id,

        "form_home":
            form_home,

        "form_away":
            form_away,

        "stats_home":
            stats_home,

        "stats_away":
            stats_away,

        "ppg_casa":
            ppg_casa,

        "ppg_trasferta":
            ppg_trasferta,

        "class_home":
            class_home,

        "class_away":
            class_away,

        "injuries_home":
            injuries_home,

        "injuries_away":
            injuries_away,

        "h2h":
            h2h,

        "europe_home":
            europe_home,

        "europe_away":
            europe_away,

        "riposo_home":
            riposo_home,

        "riposo_away":
            riposo_away,

        "fatica_home":
            fatica_home,

        "fatica_away":
            fatica_away,

        "momentum_home":
            momentum_home,

        "momentum_away":
            momentum_away,

        "prob_home":
            prob_home,

        "prob_draw":
            prob_draw,

        "prob_away":
            prob_away,

        "expected_goals":
            expected_goals,

        "over25":
            over25,

        "under25":
            under25,

        "gol":
            gol,

        "no_gol":
            no_gol,

        "migliore":
            migliore,

        "migliore_valore":
            mercati[migliore],

        "affidabilita":
            affidabilita
    }


# ============================================================
# FORMAT REPORT PARTITA
# ============================================================

def format_report_partita(
    analisi: Dict[str, Any],
    numero: int
) -> str:

    home = analisi[
        "home"
    ]

    away = analisi[
        "away"
    ]

    sh = analisi[
        "stats_home"
    ]

    sa = analisi[
        "stats_away"
    ]

    class_h = analisi[
        "class_home"
    ]

    class_a = analisi[
        "class_away"
    ]

    injuries_h = analisi[
        "injuries_home"
    ]

    injuries_a = analisi[
        "injuries_away"
    ]

    h2h = analisi[
        "h2h"
    ]

    ppg_casa = analisi[
        "ppg_casa"
    ]

    ppg_trasferta = analisi[
        "ppg_trasferta"
    ]

    riposo_h = analisi[
        "riposo_home"
    ]

    riposo_a = analisi[
        "riposo_away"
    ]

    def ppg_text(
        value
    ):

        if value is None:
            return "N/D"

        return f"{value:.2f}"

    def riposo_text(
        value
    ):

        if value is None:
            return "N/D"

        return (
            f"{value:.1f} giorni"
        )

    def momentum_text(
        value
    ):

        if value > 0:
            return f"+{value}"

        return str(value)

    # H2H
    if h2h:

        righe_h2h = []

        for match in h2h[:3]:

            dt = match.get(
                "date"
            )

            data = (
                dt.strftime(
                    "%d/%m/%Y"
                )
                if dt
                else "N/D"
            )

            righe_h2h.append(
                f"• {data}: "
                f"{match['home']} "
                f"{match['home_score']}-"
                f"{match['away_score']} "
                f"{match['away']}"
            )

        h2h_text = "\n".join(
            righe_h2h
        )

    else:

        h2h_text = (
            "Nessun confronto recente "
            "disponibile"
        )

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

{away}: {sa['sequenza']}
PPG {sa['ppg']:.2f} | GF {sa['gf']:.2f} | GS {sa['gs']:.2f}

<b>🏠 RENDIMENTO CASA / TRASFERTA</b>

{home} in casa: {ppg_text(ppg_casa)} PPG
{away} in trasferta: {ppg_text(ppg_trasferta)} PPG

<b>🏆 CLASSIFICA</b>

{home}: {format_classifica(class_h)}
{away}: {format_classifica(class_a)}

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
    campionato_key: str
) -> Optional[str]:

    if campionato_key not in CAMPIONATI:
        return None

    config = CAMPIONATI[
        campionato_key
    ]

    nome_campionato = config[
        "nome"
    ]

    print()
    print("=" * 50)

    print(
        f"📨 RICHIESTA CAMPIONATO: "
        f"{nome_campionato}"
    )

    print("=" * 50)

    partite = recupera_partite_future(
        campionato_key
    )

    if not partite:

        return (
            f"⚠️ Non sono state trovate "
            f"partite future per "
            f"<b>{nome_campionato}</b> "
            f"nei prossimi "
            f"{GIORNI_FUTURI} giorni.\n\n"
            "Se il problema persiste, "
            "controllare i log di Render "
            "per eventuali errori "
            "SofaScore."
        )

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
                campionato_key,
                indice,
                len(partite)
            )

            if analisi:

                analisi_completa.append(
                    analisi
                )

        except Exception as exc:

            print(
                f"❌ Errore analisi partita "
                f"{indice}: {exc}"
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
            f"{nome_campionato}."
        )

    print(
        f"📊 Pronostici validi creati: "
        f"{len(analisi_completa)}"
    )

    parti = []

    intestazione = f"""
<b>⚽ BOT PRONOSTICI CALCIO</b>

<b>{nome_campionato}</b>

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

    # Riepilogo
    migliori = sorted(
        analisi_completa,
        key=lambda x: (
            x[
                "migliore_valore"
            ],
            x[
                "affidabilita"
            ]
        ),
        reverse=True
    )

    righe = [
        "<b>📌 RIEPILOGO PRONOSTICI</b>",
        ""
    ]

    for analisi in migliori:

        righe.append(
            f"• <b>{analisi['home']} - "
            f"{analisi['away']}</b>"
        )

        righe.append(
            f"  {analisi['migliore']} "
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
        "✅ CREA_REPORT TERMINATO CORRETTAMENTE."
    )

    return report


# ============================================================
# INVIO TELEGRAM
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

    while len(
        testo
    ) > MAX_LEN:

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
                f"❌ Errore invio messaggio "
                f"{indice}: {exc}"
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
                    disable_web_page_preview=True
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
        "🏁 REPORT TELEGRAM INVIATO COMPLETAMENTE."
    )


# ============================================================
# MENU
# ============================================================

def crea_menu_campionati():

    markup = types.InlineKeyboardMarkup(
        row_width=2
    )

    pulsanti = []

    for key in CAMPIONATI:

        pulsanti.append(
            types.InlineKeyboardButton(
                key,
                callback_data=(
                    "campionato:"
                    + key
                )
            )
        )

    markup.add(
        *pulsanti
    )

    return markup


def campionato_da_key(
    key: str
) -> Optional[str]:

    if key in CAMPIONATI:
        return key

    return None


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def comando_start(
    message
):

    print(
        f"📩 /start ricevuto da chat "
        f"{message.chat.id}"
    )

    testo = """
<b>⚽ BOT PRONOSTICI CALCIO</b>

Benvenuto!

Seleziona il campionato da analizzare.

Il bot elaborerà:

• forma recente
• rendimento casa/trasferta
• classifica
• H2H
• impegni europei
• infortuni disponibili
• riposo/fatica
• momentum statistico
• probabilità 1X2
• Over/Under 2.5
• Gol/No Gol
• goal attesi
• affidabilità dei dati

<i>Le percentuali sono stime statistiche e non garantiscono il risultato.</i>
""".strip()

    bot.send_message(
        message.chat.id,
        testo,
        reply_markup=crea_menu_campionati()
    )


# ============================================================
# TESTO
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def messaggio_generico(
    message
):

    testo = (
        message.text or ""
    ).strip().lower()

    mappa = {

        "serie a":
            "🇮🇹 Serie A",

        "🇮🇹 serie a":
            "🇮🇹 Serie A",

        "premier league":
            "🏴 Premier League",

        "🏴 premier league":
            "🏴 Premier League",

        "la liga":
            "🇪🇸 La Liga",

        "🇪🇸 la liga":
            "🇪🇸 La Liga",

        "bundesliga":
            "🇩🇪 Bundesliga",

        "🇩🇪 bundesliga":
            "🇩🇪 Bundesliga",

        "ligue 1":
            "🇫🇷 Ligue 1",

        "🇫🇷 ligue 1":
            "🇫🇷 Ligue 1"
    }

    campionato = mappa.get(
        testo
    )

    if not campionato:

        bot.send_message(
            message.chat.id,
            "Usa /start per scegliere "
            "il campionato."
        )

        return

    avvia_analisi_chat(
        message.chat.id,
        campionato
    )


# ============================================================
# CALLBACK
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
    call.data.startswith(
        "campionato:"
    )
)
def callback_campionato(
    call
):

    campionato = call.data.split(
        ":",
        1
    )[1]

    bot.answer_callback_query(
        call.id,
        "Analisi avviata..."
    )

    avvia_analisi_chat(
        call.message.chat.id,
        campionato
    )


# ============================================================
# AVVIO ANALISI
# ============================================================

def avvia_analisi_chat(
    chat_id: int,
    campionato_key: str
):

    if campionato_key not in CAMPIONATI:

        bot.send_message(
            chat_id,
            "❌ Campionato non riconosciuto."
        )

        return

    nome = CAMPIONATI[
        campionato_key
    ]["nome"]

    try:

        bot.send_message(
            chat_id,
            (
                f"⏳ <b>Analisi {nome} "
                f"in corso...</b>\n\n"
                "Sto recuperando le partite "
                "e i dati statistici.\n"
                "Potrebbero essere necessari "
                "alcuni secondi."
            )
        )

    except Exception as exc:

        print(
            f"⚠️ Impossibile inviare messaggio "
            f"iniziale: {exc}"
        )

    def lavoro():

        try:

            print(
                f"🚀 Avvio analisi Telegram: "
                f"{nome}"
            )

            report = crea_report(
                campionato_key
            )

            if not report:

                bot.send_message(
                    chat_id,
                    "❌ Nessun report disponibile."
                )

                return

            print(
                "📊 Report generato."
            )

            print(
                "📤 Avvio invio Telegram..."
            )

            invia_report(
                chat_id,
                report
            )

            print(
                f"✅ ELABORAZIONE "
                f"{nome} TERMINATA."
            )

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

    threading.Thread(
        target=lavoro,
        daemon=True
    ).start()


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

                "status":
                    "ok",

                "service":
                    "bot-pronostici-gratis",

                "telegram":
                    "webhook",

                "polling":
                    False,

                "provider":
                    "SofaScore",

                "webhook":
                    WEBHOOK_URL
            }

            body = json.dumps(
                risposta,
                ensure_ascii=False
            ).encode(
                "utf-8"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
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
                f"{self.path}"
            )

            print(
                f"📩 Dimensione richiesta: "
                f"{len(body)} byte"
            )

            # Risposta immediata Telegram
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

            def process_update():

                try:

                    data = json.loads(
                        body.decode(
                            "utf-8"
                        )
                    )

                    print(
                        "⚙️ Elaborazione "
                        "update Telegram..."
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

                        print(
                            "✅ Update inviato "
                            "al thread."
                        )

                except Exception as exc:

                    print(
                        "❌ Errore elaborazione "
                        f"update Telegram: {exc}"
                    )

            threading.Thread(
                target=process_update,
                daemon=True
            ).start()

        except Exception as exc:

            print(
                f"❌ Errore POST webhook: "
                f"{exc}"
            )


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():

    print()
    print("=" * 50)

    print(
        "🌐 CONFIGURAZIONE WEBHOOK TELEGRAM"
    )

    print(
        f"🌐 URL webhook: "
        f"{WEBHOOK_URL}"
    )

    print("=" * 50)

    try:

        print(
            "🧹 Rimozione eventuale "
            "webhook precedente..."
        )

        bot.remove_webhook()

        time.sleep(
            1
        )

        print(
            "🔗 Impostazione nuovo webhook..."
        )

        risultato = bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True
        )

        print(
            f"✅ Webhook configurato: "
            f"{risultato}"
        )

        print(
            "🏁 Configurazione Telegram completata."
        )

        return True

    except Exception as exc:

        print(
            f"❌ Errore configurazione webhook: "
            f"{exc}"
        )

        return False


# ============================================================
# MAIN
# ============================================================

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

    print(
        "🚀 Avvio server HTTP Render..."
    )

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    print(
        "Server Render attivo."
    )

    print(
        f"🌐 Server HTTP avviato "
        f"sulla porta {PORT}"
    )

    print(
        f"🌐 Health URL: "
        f"{RENDER_EXTERNAL_URL}/health"
    )

    webhook_ok = configura_webhook()

    if not webhook_ok:

        print(
            "❌ ATTENZIONE: webhook Telegram "
            "non configurato."
        )

    print()
    print("=" * 50)

    print(
        "✅ BOT TELEGRAM OPERATIVO"
    )

    print(
        "✅ MODALITÀ: WEBHOOK"
    )

    print(
        "❌ POLLING: DISATTIVATO"
    )

    print(
        "⚽ DATI CALCISTICI: SOFASCORE"
    )

    print("=" * 50)

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print(
            "🛑 Arresto server..."
        )

    finally:

        try:
            bot.remove_webhook()
        except Exception:
            pass

        server.server_close()

        print(
            "🛑 Server arrestato."
        )


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":
    main()
