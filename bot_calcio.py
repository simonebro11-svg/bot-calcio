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

HTTP_TIMEOUT = 15

# Cache ESPN
CACHE_TTL = 30 * 60

# Giorni futuri da analizzare
GIORNI_FUTURI = 14

# Numero partite nel report
NUM_PARTITE_REPORT = 8

# Numero partite forma recente
NUM_FORM = 10

# Giorni da analizzare per la forma
GIORNI_FORM = 90

# Giorni da analizzare per il riposo
GIORNI_RIPOSO = 30


# ============================================================
# CONTROLLO CONFIGURAZIONE
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non configurato nelle Environment Variables."
    )

print("TELEGRAM_BOT_TOKEN: OK")
print("⚽ Dati calcistici: ESPN")


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)


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


def evento_terminato(
    evento: Dict[str, Any]
) -> bool:

    status = (
        evento.get("status", {})
        .get("type", {})
    )

    return bool(
        status.get("completed")
        or status.get("state") == "post"
        or status.get("name") in {
            "STATUS_FINAL",
            "STATUS_FULL_TIME"
        }
    )


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

    try:
        return int(float(score))

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


# ============================================================
# HTTP ESPN
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

    try:

        response = requests.get(
            url,
            params=params or {},
            timeout=HTTP_TIMEOUT
        )

        if response.status_code != 200:

            print(
                f"⚠️ ESPN HTTP "
                f"{response.status_code}: "
                f"{response.url}"
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
# PARTITE FUTURE
# ============================================================

def recupera_partite_future(
    campionato: str
) -> List[Dict[str, Any]]:

    print(
        f"📅 Recupero partite future: "
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
            f"📦 Uso cache: "
            f"{len(cached)} partite"
        )

        return cached

    oggi = datetime.now(
        timezone.utc
    )

    eventi = []
    ids_visti = set()

    print(
        f"🔎 Ricerca ESPN da "
        f"{oggi.strftime('%Y-%m-%d %H:%M UTC')} "
        f"per i prossimi "
        f"{GIORNI_FUTURI} giorni"
    )

    for giorno_offset in range(
        GIORNI_FUTURI
    ):

        giorno = (
            oggi
            + timedelta(
                days=giorno_offset
            )
        )

        data_str = giorno.strftime(
            "%Y%m%d"
        )

        print(
            f"   📅 ESPN "
            f"{campionato} - "
            f"data {data_str}"
        )

        data = espn_get(
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

        if not data:

            print(
                f"   ⚠️ Nessuna risposta "
                f"ESPN per {data_str}"
            )

            continue

        eventi_giorno = data.get(
            "events",
            []
        )

        print(
            f"   📊 Eventi ESPN trovati: "
            f"{len(eventi_giorno)}"
        )

        for evento in eventi_giorno:

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

                print(
                    f"   ⚠️ Data non leggibile "
                    f"per evento {event_id}"
                )

                continue

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            else:

                dt = dt.astimezone(
                    timezone.utc
                )

            if dt <= oggi:
                continue

            home, away = (
                estrai_competitors(
                    evento
                )
            )

            if not home or not away:

                print(
                    f"   ⚠️ Squadre non trovate "
                    f"per evento {event_id}"
                )

                continue

            ids_visti.add(
                event_id
            )

            evento["_datetime"] = dt

            eventi.append(
                evento
            )

            print(
                f"   ✅ "
                f"{estrai_nome_team(home)} "
                f"- "
                f"{estrai_nome_team(away)} "
                f"| "
                f"{dt.strftime('%d/%m/%Y %H:%M UTC')}"
            )

    eventi.sort(
        key=lambda x:
        x.get("_datetime")
        or datetime.max.replace(
            tzinfo=timezone.utc
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
# FORM RECENTE
# ============================================================

def recupera_form_da_eventi(
    team_name: str,
    league: str
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

    partite = []
    ids_visti = set()

    for offset in range(
        GIORNI_FORM
    ):

        giorno = (
            oggi
            - timedelta(
                days=offset
            )
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
                evento
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

            nome_norm = normalizza_nome(
                team_name
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
                continue

            dt = parse_datetime(
                evento.get(
                    "date",
                    ""
                )
            )

            if not dt:
                continue

            ids_visti.add(
                event_id
            )

            partite.append({
                "id": event_id,
                "date": dt,
                "home": nome_home,
                "away": nome_away,
                "home_score": score_home,
                "away_score": score_away,
                "team": team_name
            })

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

    for match in form:

        risultato = risultato_team(
            match,
            team_name
        )

        risultati.append(
            risultato
        )

        home = normalizza_nome(
            match["home"]
        )

        team = normalizza_nome(
            team_name
        )

        if home == team:

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


# ============================================================
# CALENDARIO COMPLETO SQUADRA
# ============================================================

def recupera_calendario_completo_squadra(
    team_id: Optional[str]
) -> Optional[List[Dict[str, Any]]]:

    if not team_id:
        return None

    cache_key = (
        f"team_all_schedule_{team_id}"
    )

    cached = cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    url = (
        f"{ESPN_BASE}/all/teams/"
        f"{team_id}/schedule"
    )

    data = espn_get_url(
        url,
        params={},
        cache_key=cache_key
    )

    if not data:
        return None

    eventi = data.get(
        "events"
    )

    if not isinstance(
        eventi,
        list
    ):
        return None

    return eventi


# ============================================================
# H2H
# ============================================================

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

    risultati = []

    calendario = (
        recupera_calendario_completo_squadra(
            home_team_id
        )
    )

    if calendario:

        home_norm = normalizza_nome(
            home_name
        )

        away_norm = normalizza_nome(
            away_name
        )

        for evento in calendario:

            if not evento_terminato(
                evento
            ):
                continue

            home, away = (
                estrai_competitors(
                    evento
                )
            )

            if not home or not away:
                continue

            eh = estrai_nome_team(
                home
            )

            ea = estrai_nome_team(
                away
            )

            coppia = {
                normalizza_nome(eh),
                normalizza_nome(ea)
            }

            richiesta = {
                home_norm,
                away_norm
            }

            if coppia != richiesta:
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
                "home": eh,
                "away": ea,
                "home_score": hs,
                "away_score": ascore
            })

    # FALLBACK
    if not risultati and league:

        oggi = datetime.now(
            timezone.utc
        )

        for offset in range(365):

            giorno = (
                oggi
                - timedelta(
                    days=offset
                )
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
                    evento
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

                coppia = {
                    normalizza_nome(eh),
                    normalizza_nome(ea)
                }

                if coppia != {
                    normalizza_nome(
                        home_name
                    ),
                    normalizza_nome(
                        away_name
                    )
                }:
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
# ============================================================

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

            return None

        data = response.json()

        lista = (
            estrai_infortuni_ricorsivo(
                data
            )
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
            f"• {nome} — {stato}"
        )

        if motivo:
            testo += (
                f" ({motivo})"
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
# EUROPA
# ============================================================

def evento_europeo(
    evento: Dict[str, Any]
) -> Optional[str]:

    testo = ""

    league_obj = evento.get(
        "league",
        {}
    )

    if isinstance(
        league_obj,
        dict
    ):

        testo += " "

        testo += str(
            league_obj.get(
                "name",
                ""
            )
        )

        testo += " "

        testo += str(
            league_obj.get(
                "slug",
                ""
            )
        )

        testo += " "

        testo += str(
            league_obj.get(
                "abbreviation",
                ""
            )
        )

    competitions = evento.get(
        "competitions",
        []
    )

    for competition in competitions:

        if not isinstance(
            competition,
            dict
        ):
            continue

        testo += " "

        testo += str(
            competition.get(
                "name",
                ""
            )
        )

        testo += " "

        testo += str(
            competition.get(
                "type",
                ""
            )
        )

    testo = normalizza_nome(
        testo
    )

    if "champions" in testo:
        return "Champions League"

    if (
        "europa league" in testo
        or "europa" in testo
    ):
        return "Europa League"

    if (
        "conference league" in testo
        or "conference" in testo
    ):
        return "Conference League"

    raw = json.dumps(
        evento,
        ensure_ascii=False
    ).lower()

    if "uefa.champions" in raw:
        return "Champions League"

    if "uefa.europa.conf" in raw:
        return "Conference League"

    if "uefa.europa" in raw:
        return "Europa League"

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

    calendario = (
        recupera_calendario_completo_squadra(
            team_id
        )
    )

    if calendario is None:

        return {
            "stato": "non_disponibile",
            "competizioni": [],
            "ultima": None
        }

    oggi = datetime.now(
        timezone.utc
    )

    limite = (
        oggi
        - timedelta(days=45)
    )

    europee = []

    for evento in calendario:

        if not evento_terminato(
            evento
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
            evento_europeo(
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
    h2h: List[Dict[str, Any]]
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

    # --------------------------------------------------------
    # Forma generale
    # --------------------------------------------------------

    differenza_forma = (
        home_ppg
        - away_ppg
    )

    home += (
        differenza_forma * 7
    )

    away -= (
        differenza_forma * 7
    )

    # --------------------------------------------------------
    # Rendimento casa / trasferta
    # --------------------------------------------------------

    if ppg_casa is not None:

        home += (
            ppg_casa - 1.5
        ) * 8

    if ppg_trasferta is not None:

        away += (
            ppg_trasferta - 1.5
        ) * 8

    # --------------------------------------------------------
    # Vantaggio campo
    # --------------------------------------------------------

    home += 5

    # --------------------------------------------------------
    # Fatica
    # --------------------------------------------------------

    home -= (
        fatigue_home * 0.08
    )

    away -= (
        fatigue_away * 0.08
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
    ) * 0.35

    away += (
        momentum_away
        - momentum_home
    ) * 0.35

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h_home = 0
    h2h_draw = 0
    h2h_away = 0

    home_norm = normalizza_nome(
        home_name
    )

    away_norm = normalizza_nome(
        away_name
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

    return (
        home / totale * 100,
        draw / totale * 100,
        away / totale * 100
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
    injuries_home: Optional[
        List[Dict[str, Any]]
    ],
    injuries_away: Optional[
        List[Dict[str, Any]]
    ],
    h2h: List[Dict[str, Any]],
    ppg_casa: Optional[float],
    ppg_trasferta: Optional[float]
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
    # FORMA
    # --------------------------------------------------------

    form_home = (
        recupera_form_da_eventi(
            home_name,
            league
        )
    )

    form_away = (
        recupera_form_da_eventi(
            away_name,
            league
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
    # INFORTUNI
    # --------------------------------------------------------

    injuries_home = recupera_infortuni(
        home_id,
        league
    )

    injuries_away = recupera_infortuni(
        away_id,
        league
    )

    # --------------------------------------------------------
    # H2H
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
    # RIPOSO / FATICA
    # --------------------------------------------------------

    riposo_home = (
        giorni_dall_ultima_partita(
            form_home
        )
    )

    riposo_away = (
        giorni_dall_ultima_partita(
            form_away
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
        h2h
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
            ppg_trasferta
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
        "injuries_home": injuries_home,
        "injuries_away": injuries_away,
        "h2h": h2h,
        "europe_home": europe_home,
        "europe_away": europe_away,
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
        competizioni
    )


# ============================================================
# FORMATTAZIONE REPORT
# ============================================================

def format_report_partita(
    analisi: Dict[str, Any],
    numero: int
) -> str:

    home = analisi["home"]
    away = analisi["away"]

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

{away}: {sa['sequenza']}
PPG {sa['ppg']:.2f} | GF {sa['gf']:.2f} | GS {sa['gs']:.2f}

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
            f"<b>{nome_campionato}</b> "
            f"nei prossimi "
            f"{GIORNI_FUTURI} giorni."
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
            f"{analisi['home']} - "
            f"{analisi['away']}"
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
        "🏁 REPORT TELEGRAM INVIATO "
        "COMPLETAMENTE."
    )


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
# HANDLER /START
# ============================================================

@bot.message_handler(
    commands=["start"]
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

• forma recente
• rendimento casa/trasferta
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
# HANDLER TESTO
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def messaggio_generico(message):

    testo = (
        message.text or ""
    ).strip().lower()

    if testo in {
        "serie a",
        "🇮🇹 serie a"
    }:

        league = "ita.1"

    elif testo in {
        "premier league",
        "🏴 premier league"
    }:

        league = "eng.1"

    elif testo in {
        "la liga",
        "🇪🇸 la liga"
    }:

        league = "esp.1"

    elif testo in {
        "bundesliga",
        "🇩🇪 bundesliga"
    }:

        league = "ger.1"

    elif testo in {
        "ligue 1",
        "🇫🇷 ligue 1"
    }:

        league = "fra.1"

    else:

        bot.send_message(
            message.chat.id,
            "Usa /start per scegliere "
            "il campionato."
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
    call.data.startswith(
        "campionato:"
    )
)
def callback_campionato(call):

    league = call.data.split(
        ":",
        1
    )[1]

    bot.answer_callback_query(
        call.id,
        "Analisi avviata..."
    )

    avvia_analisi_chat(
        call.message.chat.id,
        league
    )


# ============================================================
# AVVIO ANALISI
# ============================================================

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

        bot.send_message(
            chat_id,
            "❌ Campionato non riconosciuto."
        )

        return

    nome = CAMPIONATI[
        nome_key
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
            f"⚠️ Impossibile inviare "
            f"messaggio iniziale: {exc}"
        )

    def lavoro():

        try:

            print(
                f"🚀 Avvio analisi Telegram: "
                f"{nome}"
            )

            report = crea_report(
                nome_key
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
                "status": "ok",
                "service": "bot-pronostici-gratis",
                "telegram": "webhook",
                "polling": False,
                "webhook": WEBHOOK_URL
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

            # Risposta immediata
            # a Telegram
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

            # Elaborazione separata
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

        time.sleep(1)

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
            "🏁 Configurazione Telegram "
            "completata."
        )

        return True

    except Exception as exc:

        print(
            f"❌ Errore configurazione "
            f"webhook: {exc}"
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

    webhook_ok = (
        configura_webhook()
    )

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
        "❌ CLASSIFICA: DISATTIVATA"
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
