import os
import json
import time
import math
import threading
import http.server
from datetime import datetime, timedelta, timezone

import requests
import telebot


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
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports/soccer"

HTTP_TIMEOUT = 15

CACHE_TTL = 30 * 60

GIORNI_FUTURI = 14
NUM_PARTITE_REPORT = 8
NUM_FORM = 10


# ============================================================
# CONTROLLO TOKEN
# ============================================================

print("=" * 50)
print("⚽ BOT PRONOSTICI CALCIO")
print("Avvio applicazione Render...")
print("=" * 50)

if TELEGRAM_BOT_TOKEN:
    print("TELEGRAM_BOT_TOKEN: OK")
else:
    print("TELEGRAM_BOT_TOKEN: MANCANTE")

if FOOTBALL_API_KEY:
    print("FOOTBALL_API_KEY: OK (secondaria)")
else:
    print("FOOTBALL_API_KEY: MANCANTE (non necessaria per ESPN)")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non trovato nelle variabili di ambiente."
    )


# ============================================================
# BOT TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)


# ============================================================
# CAMPIONATI
# ============================================================

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

CACHE = {}
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
# UTILITY
# ============================================================

def safe_int(value, default=0):
    try:
        if value is None:
            return default

        if isinstance(value, bool):
            return int(value)

        if isinstance(value, str):
            value = value.replace(",", ".").strip()

        return int(float(value))
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", ".").strip()

        return float(value)
    except Exception:
        return default


def normalizza_nome(nome):
    if not nome:
        return ""

    return (
        nome.lower()
        .replace(".", "")
        .replace("-", " ")
        .replace("_", " ")
        .strip()
    )


def parse_data(data_string):
    if not data_string:
        return None

    try:
        return datetime.fromisoformat(
            data_string.replace("Z", "+00:00")
        )
    except Exception:
        return None


def oggi_utc():
    return datetime.now(timezone.utc)


def format_data_italiana(data):
    if not data:
        return "data da definire"

    try:
        return data.astimezone().strftime("%d/%m/%Y %H:%M")
    except Exception:
        return data.strftime("%d/%m/%Y %H:%M")


# ============================================================
# ESPN HTTP
# ============================================================

def espn_get(url, params=None, cache_key=None):
    if cache_key:
        cached = cache_get(cache_key)

        if cached is not None:
            return cached

    try:
        response = requests.get(
            url,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        if response.status_code != 200:
            print(
                f"⚠️ ESPN HTTP {response.status_code}: "
                f"{url}"
            )
            return {}

        data = response.json()

        if cache_key:
            cache_set(cache_key, data)

        return data

    except requests.RequestException as e:
        print(f"⚠️ Errore ESPN: {e}")
        return {}

    except Exception as e:
        print(f"⚠️ Errore parsing ESPN: {e}")
        return {}


# ============================================================
# ESTRAZIONE EVENTI ESPN
# ============================================================

def estrai_eventi(data):
    if not isinstance(data, dict):
        return []

    eventi = data.get("events")

    if isinstance(eventi, list):
        return eventi

    return []


def estrai_competizione(event):
    competitions = event.get("competitions", [])

    if not competitions:
        return {}

    if isinstance(competitions[0], dict):
        return competitions[0]

    return {}


def estrai_squadre_evento(event):
    competition = estrai_competizione(event)

    competitors = competition.get("competitors", [])

    casa = None
    trasferta = None

    for competitor in competitors:

        if not isinstance(competitor, dict):
            continue

        home_away = competitor.get("homeAway")

        if home_away == "home":
            casa = competitor

        elif home_away == "away":
            trasferta = competitor

    return casa, trasferta


def nome_squadra_competitor(competitor):
    if not competitor:
        return ""

    team = competitor.get("team", {})

    if not isinstance(team, dict):
        return ""

    return (
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or ""
    )


def id_squadra_competitor(competitor):
    if not competitor:
        return None

    team = competitor.get("team", {})

    if not isinstance(team, dict):
        return None

    value = team.get("id")

    if value is None:
        return None

    return str(value)


def score_competitor(competitor):
    if not competitor:
        return None

    value = competitor.get("score")

    if isinstance(value, dict):
        value = value.get("value")

    if value is None:
        return None

    try:
        return int(float(value))
    except Exception:
        return None


def evento_completato(event):
    status = event.get("status", {})

    if not isinstance(status, dict):
        return False

    status_type = status.get("type", {})

    if not isinstance(status_type, dict):
        return False

    if status_type.get("completed") is True:
        return True

    state = status_type.get("state", "")

    return state == "post"


def evento_data(event):
    return parse_data(event.get("date"))


def crea_info_evento(event, league_slug=None):
    casa, trasferta = estrai_squadre_evento(event)

    if not casa or not trasferta:
        return None

    nome_casa = nome_squadra_competitor(casa)
    nome_trasferta = nome_squadra_competitor(trasferta)

    if not nome_casa or not nome_trasferta:
        return None

    return {
        "id": str(event.get("id", "")),
        "date": evento_data(event),
        "home": nome_casa,
        "away": nome_trasferta,
        "home_id": id_squadra_competitor(casa),
        "away_id": id_squadra_competitor(trasferta),
        "home_score": score_competitor(casa),
        "away_score": score_competitor(trasferta),
        "completed": evento_completato(event),
        "league": league_slug or ""
    }


# ============================================================
# SCOREBOARD
# ============================================================

def recupera_scoreboard(league_slug, data_inizio, data_fine):
    key = (
        f"scoreboard:{league_slug}:"
        f"{data_inizio.strftime('%Y%m%d')}:"
        f"{data_fine.strftime('%Y%m%d')}"
    )

    url = f"{ESPN_BASE}/{league_slug}/scoreboard"

    params = {
        "dates": (
            f"{data_inizio.strftime('%Y%m%d')}-"
            f"{data_fine.strftime('%Y%m%d')}"
        )
    }

    data = espn_get(
        url,
        params=params,
        cache_key=key
    )

    risultati = []

    for event in estrai_eventi(data):
        info = crea_info_evento(
            event,
            league_slug
        )

        if info:
            risultati.append(info)

    return risultati


# ============================================================
# PARTITE FUTURE DEL CAMPIONATO
# ============================================================

def recupera_partite_future(league_slug):
    adesso = oggi_utc()

    fine = adesso + timedelta(days=GIORNI_FUTURI)

    eventi = recupera_scoreboard(
        league_slug,
        adesso,
        fine
    )

    risultati = []

    visti = set()

    for evento in eventi:

        event_id = evento.get("id")

        if event_id in visti:
            continue

        visti.add(event_id)

        data = evento.get("date")

        if not data:
            continue

        if data < adesso - timedelta(minutes=30):
            continue

        if evento.get("completed"):
            continue

        risultati.append(evento)

    risultati.sort(
        key=lambda x: x.get("date") or adesso
    )

    return risultati[:NUM_PARTITE_REPORT]


# ============================================================
# CALENDARIO SQUADRA
# ============================================================

def recupera_calendario_squadra(
    league_slug,
    team_id
):
    if not team_id:
        return []

    key = (
        f"team_schedule:"
        f"{league_slug}:{team_id}"
    )

    url = (
        f"{ESPN_BASE}/"
        f"{league_slug}/teams/{team_id}/schedule"
    )

    data = espn_get(
        url,
        cache_key=key
    )

    risultati = []

    for event in estrai_eventi(data):

        info = crea_info_evento(
            event,
            league_slug
        )

        if info:
            risultati.append(info)

    risultati.sort(
        key=lambda x: x.get("date") or oggi_utc()
    )

    return risultati


# ============================================================
# FORM SQUADRA
# ============================================================

def partita_di_squadra(evento, team_id):
    team_id = str(team_id)

    if str(evento.get("home_id")) == team_id:
        return (
            "home",
            evento.get("home_score"),
            evento.get("away_score"),
            evento.get("away")
        )

    if str(evento.get("away_id")) == team_id:
        return (
            "away",
            evento.get("away_score"),
            evento.get("home_score"),
            evento.get("home")
        )

    return None


def recupera_form_squadra(
    league_slug,
    team_id,
    numero=NUM_FORM
):
    calendario = recupera_calendario_squadra(
        league_slug,
        team_id
    )

    completate = []

    for evento in calendario:

        if not evento.get("completed"):
            continue

        risultato = partita_di_squadra(
            evento,
            team_id
        )

        if not risultato:
            continue

        sede, gol_fatti, gol_subiti, avversario = risultato

        if gol_fatti is None or gol_subiti is None:
            continue

        completate.append({
            "date": evento.get("date"),
            "home": sede == "home",
            "gf": gol_fatti,
            "ga": gol_subiti,
            "avversario": avversario,
            "result": (
                "V"
                if gol_fatti > gol_subiti
                else "P"
                if gol_fatti == gol_subiti
                else "S"
            ),
            "total_goals": gol_fatti + gol_subiti
        })

    completate.sort(
        key=lambda x: x.get("date") or oggi_utc(),
        reverse=True
    )

    return completate[:numero]


# ============================================================
# STATISTICHE FORM
# ============================================================

def calcola_statistiche_form(form):
    if not form:
        return {
            "partite": 0,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0,
            "gf": 0,
            "ga": 0,
            "gf_media": 0,
            "ga_media": 0,
            "punti": 0,
            "ppg": 0,
            "over15": 0,
            "btts": 0,
            "clean_sheet": 0,
            "failed_to_score": 0,
            "forma": ""
        }

    vittorie = sum(
        1 for x in form
        if x["result"] == "V"
    )

    pareggi = sum(
        1 for x in form
        if x["result"] == "P"
    )

    sconfitte = sum(
        1 for x in form
        if x["result"] == "S"
    )

    gf = sum(
        safe_int(x.get("gf"))
        for x in form
    )

    ga = sum(
        safe_int(x.get("ga"))
        for x in form
    )

    over15 = sum(
        1 for x in form
        if safe_int(x.get("total_goals")) >= 2
    )

    btts = sum(
        1 for x in form
        if safe_int(x.get("gf")) >= 1
        and safe_int(x.get("ga")) >= 1
    )

    clean_sheet = sum(
        1 for x in form
        if safe_int(x.get("ga")) == 0
    )

    failed_to_score = sum(
        1 for x in form
        if safe_int(x.get("gf")) == 0
    )

    punti = vittorie * 3 + pareggi

    partite = len(form)

    return {
        "partite": partite,
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte,
        "gf": gf,
        "ga": ga,
        "gf_media": gf / partite,
        "ga_media": ga / partite,
        "punti": punti,
        "ppg": punti / partite,
        "over15": over15 / partite,
        "btts": btts / partite,
        "clean_sheet": clean_sheet / partite,
        "failed_to_score": failed_to_score / partite,
        "forma": "".join(
            x["result"] for x in form
        )
    }


# ============================================================
# SPLIT CASA / TRASFERTA
# ============================================================

def calcola_split(form, casa=True):
    filtrate = [
        x for x in form
        if x.get("home") is casa
    ]

    if not filtrate:
        return None

    return calcola_statistiche_form(
        filtrate
    )


# ============================================================
# STANDINGS
# ============================================================

def estrai_stat(entry, nomi, default=0):
    stats = entry.get("stats", [])

    if not isinstance(stats, list):
        return default

    nomi_lower = {
        str(x).lower()
        for x in nomi
    }

    for stat in stats:

        if not isinstance(stat, dict):
            continue

        name = str(
            stat.get("name", "")
        ).lower()

        abbreviation = str(
            stat.get("abbreviation", "")
        ).lower()

        if (
            name in nomi_lower
            or abbreviation in nomi_lower
        ):
            value = stat.get("value")

            if value is None:
                value = stat.get(
                    "displayValue"
                )

            return safe_float(
                value,
                default
            )

    return default


def recupera_classifica(league_slug):
    key = f"standings:{league_slug}"

    url = (
        f"{ESPN_STANDINGS_BASE}/"
        f"{league_slug}/standings"
    )

    data = espn_get(
        url,
        cache_key=key
    )

    risultati = []

    def cerca_entries(obj):

        if isinstance(obj, dict):

            standings = obj.get("standings")

            if isinstance(standings, dict):
                entries = standings.get("entries")

                if isinstance(entries, list):
                    return entries

            if isinstance(standings, list):
                return standings

            entries = obj.get("entries")

            if isinstance(entries, list):
                return entries

            for value in obj.values():

                risultato = cerca_entries(value)

                if risultato:
                    return risultato

        elif isinstance(obj, list):

            for item in obj:
                risultato = cerca_entries(item)

                if risultato:
                    return risultato

        return []

    entries = cerca_entries(data)

    for entry in entries:

        if not isinstance(entry, dict):
            continue

        team = entry.get("team", {})

        if not isinstance(team, dict):
            team = {}

        team_id = team.get("id")

        nome = (
            team.get("displayName")
            or team.get("name")
            or ""
        )

        if not team_id or not nome:
            continue

        risultati.append({
            "id": str(team_id),
            "nome": nome,
            "rank": safe_int(
                estrai_stat(
                    entry,
                    ["rank"],
                    0
                )
            ),
            "points": safe_float(
                estrai_stat(
                    entry,
                    ["points", "pts"],
                    0
                )
            ),
            "games": safe_int(
                estrai_stat(
                    entry,
                    ["gamesPlayed", "gp"],
                    0
                )
            ),
            "wins": safe_int(
                estrai_stat(
                    entry,
                    ["wins", "w"],
                    0
                )
            ),
            "draws": safe_int(
                estrai_stat(
                    entry,
                    ["ties", "draws", "t"],
                    0
                )
            ),
            "losses": safe_int(
                estrai_stat(
                    entry,
                    ["losses", "l"],
                    0
                )
            ),
            "gf": safe_int(
                estrai_stat(
                    entry,
                    ["goalsFor", "gf"],
                    0
                )
            ),
            "ga": safe_int(
                estrai_stat(
                    entry,
                    ["goalsAgainst", "ga"],
                    0
                )
            )
        })

    return risultati


def trova_classifica_squadra(
    standings,
    team_id
):
    if not standings or not team_id:
        return None

    team_id = str(team_id)

    for item in standings:

        if str(item.get("id")) == team_id:
            return item

    return None


# ============================================================
# INFORMAZIONI SQUADRA
# ============================================================

def recupera_infortuni(
    league_slug,
    team_id
):
    if not team_id:
        return []

    key = (
        f"injuries:"
        f"{league_slug}:{team_id}"
    )

    url = (
        f"{ESPN_BASE}/"
        f"{league_slug}/teams/"
        f"{team_id}/injuries"
    )

    data = espn_get(
        url,
        cache_key=key
    )

    trovati = []

    def percorri(obj):

        if isinstance(obj, dict):

            if (
                "athlete" in obj
                or "player" in obj
            ):
                trovati.append(obj)

            for value in obj.values():
                percorri(value)

        elif isinstance(obj, list):

            for item in obj:
                percorri(item)

    percorri(data)

    unici = {}
    sospesi = 0

    for item in trovati:

        athlete = (
            item.get("athlete")
            or item.get("player")
            or {}
        )

        if not isinstance(athlete, dict):
            athlete = {}

        athlete_id = (
            athlete.get("id")
            or athlete.get("displayName")
            or athlete.get("fullName")
            or athlete.get("shortName")
        )

        if not athlete_id:
            continue

        nome = (
            athlete.get("displayName")
            or athlete.get("fullName")
            or athlete.get("shortName")
            or "Giocatore"
        )

        testo = json.dumps(
            item,
            ensure_ascii=False
        ).lower()

        is_sospeso = (
            "suspension" in testo
            or "suspended" in testo
            or "squalific" in testo
        )

        unici[str(athlete_id)] = {
            "nome": nome,
            "sospeso": is_sospeso
        }

    for item in unici.values():

        if item["sospeso"]:
            sospesi += 1

    return [
        {
            **item,
            "id": key
        }
        for key, item in unici.items()
    ]


def analizza_infortuni(infortuni):
    if not infortuni:
        return {
            "totale": 0,
            "sospesi": 0,
            "peso": 0
        }

    totale = len(infortuni)

    sospesi = sum(
        1
        for x in infortuni
        if x.get("sospeso")
    )

    peso = min(
        6.0,
        totale * 0.8
        + sospesi * 0.8
    )

    return {
        "totale": totale,
        "sospesi": sospesi,
        "peso": peso
    }


# ============================================================
# H2H
# ============================================================

def recupera_h2h(
    league_slug,
    team_id,
    opponent_id
):
    if not team_id or not opponent_id:
        return []

    calendario = recupera_calendario_squadra(
        league_slug,
        team_id
    )

    risultati = []

    for evento in calendario:

        if not evento.get("completed"):
            continue

        if (
            str(evento.get("home_id")) == str(opponent_id)
            or
            str(evento.get("away_id")) == str(opponent_id)
        ):
            risultati.append(evento)

    risultati.sort(
        key=lambda x: x.get("date") or oggi_utc(),
        reverse=True
    )

    return risultati[:5]


def calcola_h2h(
    h2h,
    team_id
):
    if not h2h:
        return {
            "partite": 0,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0
        }

    vittorie = 0
    pareggi = 0
    sconfitte = 0

    for evento in h2h:

        risultato = partita_di_squadra(
            evento,
            team_id
        )

        if not risultato:
            continue

        _, gf, ga, _ = risultato

        if gf > ga:
            vittorie += 1
        elif gf == ga:
            pareggi += 1
        else:
            sconfitte += 1

    return {
        "partite": len(h2h),
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte
    }


# ============================================================
# FATICA / CALENDARIO EUROPEO
# ============================================================

def recupera_eventi_europei_squadra(
    team_id,
    data_centro
):
    if not team_id:
        return []

    risultati = []

    for league_slug in COMPETIZIONI_EUROPEE:

        calendario = recupera_calendario_squadra(
            league_slug,
            team_id
        )

        for evento in calendario:

            data = evento.get("date")

            if not data:
                continue

            if (
                data >= data_centro - timedelta(days=21)
                and
                data <= data_centro + timedelta(days=21)
            ):
                risultati.append(evento)

    return risultati


def analizza_fatica(
    league_slug,
    team_id,
    data_partita
):
    calendario = recupera_calendario_squadra(
        league_slug,
        team_id
    )

    if not calendario:
        return {
            "score": 0,
            "riposo": None,
            "partite_7": 0,
            "partite_14": 0,
            "europea_vicina": False
        }

    passate = []

    for evento in calendario:

        if not evento.get("completed"):
            continue

        data = evento.get("date")

        if not data:
            continue

        if data < data_partita:
            passate.append(evento)

    passate.sort(
        key=lambda x: x.get("date") or data_partita,
        reverse=True
    )

    score = 0

    partite_7 = 0
    partite_14 = 0

    for evento in passate:

        data = evento.get("date")

        if not data:
            continue

        giorni = (
            data_partita - data
        ).total_seconds() / 86400

        if giorni <= 7:
            partite_7 += 1

        if giorni <= 14:
            partite_14 += 1

    riposo = None

    if passate:

        ultima = passate[0].get("date")

        if ultima:
            riposo = (
                data_partita - ultima
            ).total_seconds() / 86400

            if riposo < 2:
                score += 20
            elif riposo < 3:
                score += 14
            elif riposo < 4:
                score += 7

    if partite_7 >= 3:
        score += 12

    if partite_14 >= 5:
        score += 10

    europee = recupera_eventi_europei_squadra(
        team_id,
        data_partita
    )

    europea_vicina = False

    for evento in europee:

        data = evento.get("date")

        if not data:
            continue

        differenza = abs(
            (
                data - data_partita
            ).total_seconds()
        ) / 86400

        if differenza <= 5:
            europea_vicina = True
            score += 8
            break

    return {
        "score": min(score, 40),
        "riposo": riposo,
        "partite_7": partite_7,
        "partite_14": partite_14,
        "europea_vicina": europea_vicina
    }


# ============================================================
# MOMENTO / MOTIVAZIONE MISURABILE
# ============================================================

def analizza_momento(stats):
    if not stats or stats["partite"] == 0:
        return 0

    score = 0

    ppg = stats["ppg"]

    if ppg >= 2.2:
        score += 12
    elif ppg >= 1.7:
        score += 7
    elif ppg >= 1.3:
        score += 3
    elif ppg < 0.9:
        score -= 8

    if stats["gf_media"] >= 1.8:
        score += 6

    if stats["ga_media"] <= 0.9:
        score += 6

    if stats["clean_sheet"] >= 0.4:
        score += 4

    if stats["failed_to_score"] >= 0.5:
        score -= 6

    return max(-15, min(20, score))


# ============================================================
# PROBABILITA'
# ============================================================

def probabilita_da_forze(
    casa,
    trasferta,
    class_casa=None,
    class_trasferta=None,
    fatica_casa=None,
    fatica_trasferta=None,
    infortuni_casa=None,
    infortuni_trasferta=None,
    h2h_casa=None
):
    forza_casa = 0.0
    forza_trasferta = 0.0

    # --------------------------------------------------------
    # FORM
    # --------------------------------------------------------

    forza_casa += (
        casa["ppg"] * 12
        + casa["gf_media"] * 5
        - casa["ga_media"] * 4
    )

    forza_trasferta += (
        trasferta["ppg"] * 12
        + trasferta["gf_media"] * 5
        - trasferta["ga_media"] * 4
    )

    # --------------------------------------------------------
    # VANTAGGIO CASA
    # --------------------------------------------------------

    forza_casa += 5

    # --------------------------------------------------------
    # MOMENTO
    # --------------------------------------------------------

    forza_casa += analizza_momento(casa)
    forza_trasferta += analizza_momento(trasferta)

    # --------------------------------------------------------
    # CLASSIFICA
    # --------------------------------------------------------

    if class_casa and class_trasferta:

        rank_casa = safe_int(
            class_casa.get("rank"),
            0
        )

        rank_trasferta = safe_int(
            class_trasferta.get("rank"),
            0
        )

        if rank_casa > 0 and rank_trasferta > 0:

            differenza = rank_trasferta - rank_casa

            forza_casa += max(
                -8,
                min(8, differenza * 0.7)
            )

    # --------------------------------------------------------
    # FATICA
    # --------------------------------------------------------

    if fatica_casa:
        forza_casa -= (
            fatica_casa.get("score", 0) * 0.20
        )

    if fatica_trasferta:
        forza_trasferta -= (
            fatica_trasferta.get("score", 0) * 0.20
        )

    # --------------------------------------------------------
    # INFORTUNI
    # --------------------------------------------------------

    if infortuni_casa:
        forza_casa -= (
            infortuni_casa.get("peso", 0)
        )

    if infortuni_trasferta:
        forza_trasferta -= (
            infortuni_trasferta.get("peso", 0)
        )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    if h2h_casa:
        partite = h2h_casa.get("partite", 0)

        if partite > 0:

            vittorie = h2h_casa.get(
                "vittorie",
                0
            )

            sconfitte = h2h_casa.get(
                "sconfitte",
                0
            )

            forza_casa += (
                vittorie - sconfitte
            ) * 1.5

    # --------------------------------------------------------
    # NORMALIZZAZIONE
    # --------------------------------------------------------

    totale = (
        abs(forza_casa)
        + abs(forza_trasferta)
        + 30
    )

    differenza = (
        forza_casa - forza_trasferta
    )

    p_casa = 0.333 + differenza / totale
    p_trasferta = 0.333 - differenza / totale

    p_casa = max(0.08, min(0.80, p_casa))
    p_trasferta = max(0.08, min(0.80, p_trasferta))

    p_x = 1 - p_casa - p_trasferta

    p_x = max(0.10, min(0.45, p_x))

    totale_prob = (
        p_casa
        + p_x
        + p_trasferta
    )

    p_casa /= totale_prob
    p_x /= totale_prob
    p_trasferta /= totale_prob

    return {
        "1": p_casa,
        "X": p_x,
        "2": p_trasferta
    }


# ============================================================
# PRONOSTICO OVER / BTTS
# ============================================================

def calcola_goal_probabilita(
    casa,
    trasferta
):
    media_gol = (
        casa["gf_media"]
        + casa["ga_media"]
        + trasferta["gf_media"]
        + trasferta["ga_media"]
    ) / 2

    over15 = (
        casa["over15"]
        + trasferta["over15"]
    ) / 2

    btts = (
        casa["btts"]
        + trasferta["btts"]
    ) / 2

    # Correzione leggera basata sui gol medi
    over15 = (
        over15 * 0.65
        + min(0.95, media_gol / 2.4) * 0.35
    )

    btts = (
        btts * 0.70
        + min(
            0.90,
            (
                casa["gf_media"]
                + trasferta["gf_media"]
            ) / 3.0
        ) * 0.30
    )

    over15 = max(
        0.35,
        min(0.95, over15)
    )

    btts = max(
        0.25,
        min(0.90, btts)
    )

    return over15, btts


# ============================================================
# GENERAZIONE PRONOSTICO
# ============================================================

def genera_pronostico_avanzato(
    dati_casa,
    dati_trasferta
):
    prob = probabilita_da_forze(
        dati_casa["form"],
        dati_trasferta["form"],
        dati_casa.get("classifica"),
        dati_trasferta.get("classifica"),
        dati_casa.get("fatica"),
        dati_trasferta.get("fatica"),
        dati_casa.get("infortuni"),
        dati_trasferta.get("infortuni"),
        dati_casa.get("h2h")
    )

    p1 = prob["1"]
    px = prob["X"]
    p2 = prob["2"]

    if p1 >= p2:
        segno_principale = "1"
        p_principale = p1
        altro = px
    else:
        segno_principale = "2"
        p_principale = p2
        altro = px

    # --------------------------------------------------------
    # DOPPIA CHANCE
    # --------------------------------------------------------

    if p1 >= p2:

        if p1 + px >= 0.67:
            doppia_chance = "1X"
        else:
            doppia_chance = "X2"

    else:

        if p2 + px >= 0.67:
            doppia_chance = "X2"
        else:
            doppia_chance = "1X"

    # --------------------------------------------------------
    # OVER / BTTS
    # --------------------------------------------------------

    over15, btts = calcola_goal_probabilita(
        dati_casa["form"],
        dati_trasferta["form"]
    )

    pronostico_over = (
        "OVER 1.5"
        if over15 >= 0.58
        else "UNDER 1.5"
    )

    pronostico_btts = (
        "GOL"
        if btts >= 0.56
        else "NO GOL"
    )

    # --------------------------------------------------------
    # AFFIDABILITA'
    # --------------------------------------------------------

    affidabilita = 55

    differenza = abs(p1 - p2)

    affidabilita += int(
        min(15, differenza * 45)
    )

    if dati_casa["form"]["partite"] >= 5:
        affidabilita += 5

    if dati_trasferta["form"]["partite"] >= 5:
        affidabilita += 5

    if (
        dati_casa.get("classifica")
        and dati_trasferta.get("classifica")
    ):
        affidabilita += 4

    if (
        dati_casa.get("fatica", {}).get("score", 0)
        >= 20
    ):
        affidabilita -= 2

    if (
        dati_trasferta.get("fatica", {}).get("score", 0)
        >= 20
    ):
        affidabilita -= 2

    # Dati insufficienti
    if (
        dati_casa["form"]["partite"] < 3
        or dati_trasferta["form"]["partite"] < 3
    ):
        affidabilita -= 12

    affidabilita = max(
        50,
        min(86, affidabilita)
    )

    # --------------------------------------------------------
    # PRONOSTICO MIGLIORE
    # --------------------------------------------------------

    if (
        p1 + px >= 0.68
        and p1 >= p2
    ):
        migliore = "1X"

    elif (
        p2 + px >= 0.68
        and p2 > p1
    ):
        migliore = "X2"

    elif p1 >= 0.56:
        migliore = "1"

    elif p2 >= 0.56:
        migliore = "2"

    else:
        migliore = doppia_chance

    return {
        "probabilita": prob,
        "migliore": migliore,
        "over": pronostico_over,
        "btts": pronostico_btts,
        "affidabilita": affidabilita,
        "over_prob": over15,
        "btts_prob": btts
    }


# ============================================================
# FORMATTA REPORT
# ============================================================

def formatta_pronostico(
    partita,
    pronostico
):
    return (
        f"⚽ <b>{partita['home']} - "
        f"{partita['away']}</b>\n\n"

        f"🔮 <b>PRONOSTICO MIGLIORE</b>\n"
        f"{pronostico['migliore']}\n\n"

        f"📈 <b>{pronostico['over']}</b>\n"
        f"⚽ <b>{pronostico['btts']}</b>\n\n"

        f"🎯 <b>AFFIDABILITÀ: "
        f"{pronostico['affidabilita']}%</b>"
    )


# ============================================================
# ANALISI COMPLETA PARTITA
# ============================================================

def analizza_partita(
    partita,
    league_slug
):
    home_id = partita.get("home_id")
    away_id = partita.get("away_id")
    data_partita = partita.get("date")

    if not home_id or not away_id:
        return None

    if not data_partita:
        return None

    # --------------------------------------------------------
    # FORM
    # --------------------------------------------------------

    form_casa_lista = recupera_form_squadra(
        league_slug,
        home_id
    )

    form_trasferta_lista = recupera_form_squadra(
        league_slug,
        away_id
    )

    form_casa = calcola_statistiche_form(
        form_casa_lista
    )

    form_trasferta = calcola_statistiche_form(
        form_trasferta_lista
    )

    # --------------------------------------------------------
    # CLASSIFICA
    # --------------------------------------------------------

    standings = recupera_classifica(
        league_slug
    )

    class_casa = trova_classifica_squadra(
        standings,
        home_id
    )

    class_trasferta = trova_classifica_squadra(
        standings,
        away_id
    )

    # --------------------------------------------------------
    # INFORTUNI
    # --------------------------------------------------------

    infortuni_casa_raw = recupera_infortuni(
        league_slug,
        home_id
    )

    infortuni_trasferta_raw = recupera_infortuni(
        league_slug,
        away_id
    )

    infortuni_casa = analizza_infortuni(
        infortuni_casa_raw
    )

    infortuni_trasferta = analizza_infortuni(
        infortuni_trasferta_raw
    )

    # --------------------------------------------------------
    # FATICA
    # --------------------------------------------------------

    fatica_casa = analizza_fatica(
        league_slug,
        home_id,
        data_partita
    )

    fatica_trasferta = analizza_fatica(
        league_slug,
        away_id,
        data_partita
    )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h_casa_raw = recupera_h2h(
        league_slug,
        home_id,
        away_id
    )

    h2h_casa = calcola_h2h(
        h2h_casa_raw,
        home_id
    )

    # --------------------------------------------------------
    # DATI COMPLETI
    # --------------------------------------------------------

    dati_casa = {
        "form": form_casa,
        "classifica": class_casa,
        "fatica": fatica_casa,
        "infortuni": infortuni_casa,
        "h2h": h2h_casa
    }

    dati_trasferta = {
        "form": form_trasferta,
        "classifica": class_trasferta,
        "fatica": fatica_trasferta,
        "infortuni": infortuni_trasferta
    }

    # --------------------------------------------------------
    # PRONOSTICO
    # --------------------------------------------------------

    pronostico = genera_pronostico_avanzato(
        dati_casa,
        dati_trasferta
    )

    return pronostico


# ============================================================
# REPORT CAMPIONATO
# ============================================================

def crea_report(
    nome_campionato,
    league_slug
):
    print("=" * 42)
    print(
        f"🚨 CREAREPORT: "
        f"{nome_campionato}"
    )
    print("=" * 42)

    partite = recupera_partite_future(
        league_slug
    )

    if not partite:
        return (
            "⚠️ <b>Nessuna partita trovata.</b>\n\n"
            "ESPN non ha restituito partite "
            "future per questo campionato "
            "nei prossimi giorni."
        )

    risultati = []

    for indice, partita in enumerate(partite):

        print(
            f"🔎 Analisi "
            f"{indice + 1}/{len(partite)}: "
            f"{partita['home']} - "
            f"{partita['away']}"
        )

        try:

            pronostico = analizza_partita(
                partita,
                league_slug
            )

            if not pronostico:
                continue

            risultati.append(
                formatta_pronostico(
                    partita,
                    pronostico
                )
            )

        except Exception as e:

            print(
                f"⚠️ Errore analisi "
                f"{partita['home']} - "
                f"{partita['away']}: {e}"
            )

    if not risultati:
        return (
            "⚠️ <b>Non sono riuscito "
            "a costruire pronostici affidabili.</b>\n\n"
            "I dati delle partite non sono "
            "sufficienti in questo momento."
        )

    intestazione = (
        f"⚽ <b>PRONOSTICI "
        f"{nome_campionato.upper()}</b>\n\n"
        f"📅 Prossime partite\n\n"
    )

    return intestazione + "\n\n".join(
        risultati
    )


# ============================================================
# INVIO MESSAGGI DIVISI
# ============================================================

def invia_report_diviso(
    chat_id,
    testo
):
    limite = 3900

    if len(testo) <= limite:
        bot.send_message(
            chat_id,
            testo
        )
        return

    parti = []

    blocchi = testo.split("\n\n")

    corrente = ""

    for blocco in blocchi:

        if len(corrente) + len(blocco) + 2 <= limite:

            if corrente:
                corrente += "\n\n"

            corrente += blocco

        else:

            if corrente:
                parti.append(corrente)

            corrente = blocco

    if corrente:
        parti.append(corrente)

    for parte in parti:

        bot.send_message(
            chat_id,
            parte
        )

        time.sleep(0.3)


# ============================================================
# TELEGRAM - START
# ============================================================

@bot.message_handler(commands=["start"])
def comando_start(message):

    testo = (
        "⚽ <b>Benvenuto nel Bot Pronostici Calcio!</b>\n\n"

        "Posso analizzare le principali "
        "competizioni europee usando dati "
        "aggiornati.\n\n"

        "📊 Analizzo automaticamente:\n"
        "• forma recente\n"
        "• rendimento casa/trasferta\n"
        "• gol fatti e subiti\n"
        "• classifica\n"
        "• calendario\n"
        "• giorni di riposo\n"
        "• congestione delle partite\n"
        "• impegni europei\n"
        "• infortuni\n"
        "• squalifiche disponibili\n"
        "• scontri diretti\n"
        "• vantaggio campo\n\n"

        "🔮 <b>Comandi disponibili</b>\n\n"

        "/campionati - Lista campionati\n"
        "/help - Aiuto\n\n"

        "Oppure scrivi direttamente:\n"
        "🇮🇹 Serie A\n"
        "🏴 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1"
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# TELEGRAM - HELP
# ============================================================

@bot.message_handler(commands=["help"])
def comando_help(message):

    testo = (
        "ℹ️ <b>COME USARE IL BOT</b>\n\n"

        "Scrivi il nome del campionato "
        "che vuoi analizzare.\n\n"

        "Esempio:\n"
        "<b>Serie A</b>\n\n"

        "Il bot cercherà le prossime partite "
        "e creerà automaticamente il pronostico."
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# TELEGRAM - CAMPIONATI
# ============================================================

@bot.message_handler(commands=["campionati"])
def comando_campionati(message):

    testo = (
        "🏆 <b>CAMPIONATI DISPONIBILI</b>\n\n"
        "🇮🇹 Serie A\n"
        "🏴 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "Scrivi il nome del campionato."
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# RICONOSCIMENTO CAMPIONATO
# ============================================================

def trova_campionato(testo):
    if not testo:
        return None

    valore = normalizza_nome(testo)

    mapping = {
        "serie a": "🇮🇹 Serie A",
        "italia": "🇮🇹 Serie A",
        "italian": "🇮🇹 Serie A",

        "premier league": "🏴 Premier League",
        "premier": "🏴 Premier League",
        "inghilterra": "🏴 Premier League",

        "la liga": "🇪🇸 La Liga",
        "liga": "🇪🇸 La Liga",
        "spagna": "🇪🇸 La Liga",

        "bundesliga": "🇩🇪 Bundesliga",
        "germania": "🇩🇪 Bundesliga",

        "ligue 1": "🇫🇷 Ligue 1",
        "francia": "🇫🇷 Ligue 1"
    }

    return mapping.get(valore)


# ============================================================
# TELEGRAM - MESSAGGI TESTUALI
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def messaggio_testuale(message):

    testo = (
        message.text or ""
    ).strip()

    campionato = trova_campionato(
        testo
    )

    if not campionato:

        bot.send_message(
            message.chat.id,
            "⚠️ Non ho riconosciuto il campionato.\n\n"
            "Scrivi /campionati per vedere "
            "quelli disponibili."
        )

        return

    config = CAMPIONATI.get(
        campionato
    )

    if not config:

        bot.send_message(
            message.chat.id,
            "⚠️ Campionato non configurato."
        )

        return

    bot.send_message(
        message.chat.id,
        (
            f"🔎 <b>Analizzo "
            f"{config['nome']}...</b>\n\n"
            "⏳ Sto controllando forma, "
            "classifica, calendario, "
            "infortuni e impegni europei."
        )
    )

    try:

        report = crea_report(
            config["nome"],
            config["espn"]
        )

        invia_report_diviso(
            message.chat.id,
            report
        )

    except Exception as e:

        print(
            f"❌ Errore creazione report: {e}"
        )

        bot.send_message(
            message.chat.id,
            (
                "❌ <b>Errore durante l'analisi.</b>\n\n"
                "Riprova tra qualche minuto."
            )
        )


# ============================================================
# SERVER HTTP RENDER
# ============================================================

class HealthHandler(
    http.server.BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):
        return

    def do_GET(self):

        if self.path in ["/", "/health"]:

            risposta = (
                "BOT PRONOSTICI CALCIO - ONLINE"
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(resposta.encode("utf-8")))
            )

            self.end_headers()

            self.wfile.write(
                risposta.encode("utf-8")
            )

            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        if self.path != WEBHOOK_PATH:

            self.send_response(404)
            self.end_headers()

            return

        try:

            print(
                f"📩 POST RICEVUTA: "
                f"{self.path}"
            )

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            print(
                f"📩 Dimensione richiesta: "
                f"{content_length} byte"
            )

            body = self.rfile.read(
                content_length
            )

            update_data = json.loads(
                body.decode("utf-8")
            )

            update = telebot.types.Update.de_json(
                json.dumps(update_data)
            )

            print(
                "⚙️ Elaborazione update Telegram..."
            )

            def elabora_update():

                try:

                    bot.process_new_updates(
                        [update]
                    )

                    print(
                        "✅ Update Telegram elaborato."
                    )

                except Exception as e:

                    print(
                        f"❌ Errore "
                        f"processamento update: {e}"
                    )

            threading.Thread(
                target=elabora_update,
                daemon=True
            ).start()

            print(
                "✅ Update inviato al thread."
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(
                b"OK"
            )

        except Exception as e:

            print(
                f"❌ Errore webhook: {e}"
            )

            self.send_response(500)
            self.end_headers()


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():

    try:

        print("=" * 50)
        print("CONFIGURAZIONE TELEGRAM")
        print("=" * 50)

        print(
            f"🌐 Webhook URL: "
            f"{WEBHOOK_URL}"
        )

        # Rimuove eventuali vecchi webhook
        try:

            bot.remove_webhook()

            print(
                "🧹 Vecchio webhook rimosso."
            )

            time.sleep(1)

        except Exception as e:

            print(
                f"⚠️ Impossibile rimuovere "
                f"vecchio webhook: {e}"
            )

        # Imposta il nuovo webhook
        risultato = bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True
        )

        print(
            f"✅ Webhook configurato: "
            f"{risultato}"
        )

        info = bot.get_webhook_info()

        print(
            f"📡 Webhook Telegram: "
            f"{info.url}"
        )

        print(
            f"📦 Pending updates: "
            f"{info.pending_update_count}"
        )

    except Exception as e:

        print(
            f"❌ Errore configurazione "
            f"webhook: {e}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 50)
    print("SERVER RENDER ATTIVO")
    print("=" * 50)

    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    thread_server = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    thread_server.start()

    print(
        f"🌐 Server HTTP avviato "
        f"sulla porta {PORT}"
    )

    time.sleep(2)

    configura_webhook()

    print("=" * 50)
    print("🚀 BOT ONLINE")
    print("=" * 50)

    # IMPORTANTE:
    # NON usare infinity_polling().
    #
    # Telegram comunica con il bot attraverso
    # il webhook HTTP.
    #
    # Il processo deve semplicemente rimanere attivo.

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
