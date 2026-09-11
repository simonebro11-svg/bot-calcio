import os
import json
import time
import math
import threading
import http.server
import traceback

from datetime import datetime, timedelta, timezone

import requests
import telebot


# ============================================================
# CONFIGURAZIONE RENDER
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

ESPN_BASE = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer"
)

ESPN_STANDINGS_BASE = (
    "https://site.api.espn.com/apis/v2/sports/soccer"
)

HTTP_TIMEOUT = 15
CACHE_TTL = 30 * 60

GIORNI_FUTURI = 14
NUM_PARTITE_REPORT = 8
NUM_FORM = 10


# ============================================================
# CONTROLLO CONFIGURAZIONE
# ============================================================

print("==========================================")
print("⚽ BOT PRONOSTICI CALCIO")
print("Avvio applicazione Render...")
print("==========================================")

if TELEGRAM_BOT_TOKEN:
    print("TELEGRAM_BOT_TOKEN: OK")
else:
    print("❌ TELEGRAM_BOT_TOKEN: MANCANTE")

if FOOTBALL_API_KEY:
    print("FOOTBALL_API_KEY: OK")
else:
    print("⚠️ FOOTBALL_API_KEY: MANCANTE")

print("==========================================")


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non configurato su Render."
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
        elemento = CACHE.get(key)

        if not elemento:
            return None

        timestamp, valore = elemento

        if time.time() - timestamp > CACHE_TTL:
            del CACHE[key]
            return None

        return valore


def cache_set(key, valore):
    with CACHE_LOCK:
        CACHE[key] = (time.time(), valore)


# ============================================================
# FUNZIONI DI SUPPORTO
# ============================================================

def safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def normalizza_nome(nome):
    if not nome:
        return ""

    return (
        str(nome)
        .lower()
        .replace("-", " ")
        .replace("_", " ")
        .replace(".", "")
        .strip()
    )


def parse_data(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


def oggi_utc():
    return datetime.now(timezone.utc)


def format_data_italiana(value):
    dt = parse_data(value)

    if not dt:
        return "Data non disponibile"

    mesi = [
        "gennaio",
        "febbraio",
        "marzo",
        "aprile",
        "maggio",
        "giugno",
        "luglio",
        "agosto",
        "settembre",
        "ottobre",
        "novembre",
        "dicembre"
    ]

    return (
        f"{dt.day} {mesi[dt.month - 1]} "
        f"{dt.year} alle {dt.strftime('%H:%M')}"
    )


# ============================================================
# RICHIESTE ESPN
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
            timeout=HTTP_TIMEOUT
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

    except requests.RequestException as exc:
        print(
            f"⚠️ Errore richiesta ESPN: {exc}"
        )
        return {}

    except ValueError as exc:
        print(
            f"⚠️ JSON ESPN non valido: {exc}"
        )
        return {}

    except Exception as exc:
        print(
            f"⚠️ Errore ESPN: {exc}"
        )
        return {}


# ============================================================
# EVENTI ESPN
# ============================================================

def estrai_eventi(data):
    if not isinstance(data, dict):
        return []

    eventi = data.get("events", [])

    if isinstance(eventi, list):
        return eventi

    return []


def estrai_competizione(evento):
    try:
        competitions = evento.get(
            "competitions",
            []
        )

        if not competitions:
            return ""

        competition = competitions[0]

        league = competition.get(
            "league",
            {}
        )

        if league.get("slug"):
            return league["slug"]

        if league.get("name"):
            return league["name"]

        competitors = competition.get(
            "competitors",
            []
        )

        if competitors:
            team = competitors[0].get(
                "team",
                {}
            )

            return team.get("league", "")

    except Exception:
        pass

    return ""


def estrai_squadre_evento(evento):

    try:
        competition = evento.get(
            "competitions",
            [])[0]

        competitors = competition.get(
            "competitors",
            []
        )

        if len(competitors) < 2:
            return None, None

        home = None
        away = None

        for competitor in competitors:

            home_away = competitor.get(
                "homeAway"
            )

            if home_away == "home":
                home = competitor

            elif home_away == "away":
                away = competitor

        if home is None or away is None:
            home = competitors[0]
            away = competitors[1]

        return home, away

    except Exception:
        return None, None


def nome_squadra_competitor(competitor):

    if not competitor:
        return ""

    team = competitor.get(
        "team",
        {}
    )

    return (
        team.get("displayName")
        or team.get("name")
        or team.get("shortDisplayName")
        or competitor.get("displayName")
        or ""
    )


def id_squadra_competitor(competitor):

    if not competitor:
        return ""

    team = competitor.get(
        "team",
        {}
    )

    return str(
        team.get("id")
        or competitor.get("id")
        or ""
    )


def score_competitor(competitor):

    if not competitor:
        return 0

    return safe_int(
        competitor.get("score"),
        0
    )


def evento_completato(evento):

    try:
        status = (
            evento
            .get("status", {})
            .get("type", {})
        )

        return bool(
            status.get("completed")
        )

    except Exception:
        return False


def evento_data(evento):

    return evento.get("date")


def crea_info_evento(evento):

    home, away = estrai_squadre_evento(evento)

    if not home or not away:
        return None

    return {
        "id": evento.get("id"),
        "date": evento_data(evento),
        "home": nome_squadra_competitor(home),
        "away": nome_squadra_competitor(away),
        "home_id": id_squadra_competitor(home),
        "away_id": id_squadra_competitor(away),
        "home_score": score_competitor(home),
        "away_score": score_competitor(away),
        "completed": evento_completato(evento),
        "competition": estrai_competizione(evento)
    }


# ============================================================
# SCOREBOARD
# ============================================================

def recupera_scoreboard(league_slug, giorni_indietro=30):

    risultati = []

    oggi = oggi_utc().date()

    inizio = oggi - timedelta(
        days=giorni_indietro
    )

    fine = oggi + timedelta(
        days=GIORNI_FUTURI
    )

    giorno = inizio

    while giorno <= fine:

        data_str = giorno.strftime("%Y%m%d")

        url = (
            f"{ESPN_BASE}/"
            f"{league_slug}/"
            f"scoreboard"
        )

        data = espn_get(
            url,
            params={"dates": data_str},
            cache_key=(
                f"scoreboard:"
                f"{league_slug}:"
                f"{data_str}"
            )
        )

        for evento in estrai_eventi(data):

            info = crea_info_evento(evento)

            if info:
                risultati.append(info)

        giorno += timedelta(days=1)

    return risultati


def recupera_partite_future(league_slug):

    oggi = oggi_utc().date()
    limite = oggi + timedelta(
        days=GIORNI_FUTURI
    )

    partite = []

    giorno = oggi

    while giorno <= limite:

        data_str = giorno.strftime("%Y%m%d")

        url = (
            f"{ESPN_BASE}/"
            f"{league_slug}/"
            f"scoreboard"
        )

        data = espn_get(
            url,
            params={"dates": data_str},
            cache_key=(
                f"future:"
                f"{league_slug}:"
                f"{data_str}"
            )
        )

        for evento in estrai_eventi(data):

            info = crea_info_evento(evento)

            if not info:
                continue

            if info["completed"]:
                continue

            data_evento = parse_data(
                info["date"]
            )

            if not data_evento:
                continue

            if data_evento.date() < oggi:
                continue

            if data_evento.date() > limite:
                continue

            partite.append(info)

        giorno += timedelta(days=1)

    # elimina duplicati
    uniche = {}

    for partita in partite:
        chiave = partita.get("id")

        if not chiave:
            chiave = (
                normalizza_nome(
                    partita["home"]
                )
                + "-"
                + normalizza_nome(
                    partita["away"]
                )
                + "-"
                + str(partita["date"])
            )

        uniche[chiave] = partita

    partite = list(uniche.values())

    partite.sort(
        key=lambda x: (
            parse_data(x["date"])
            or datetime.max.replace(
                tzinfo=timezone.utc
            )
        )
    )

    return partite


# ============================================================
# CALENDARIO SQUADRA
# ============================================================

def recupera_calendario_squadra(
    league_slug,
    team_id,
    giorni=120
):

    if not team_id:
        return []

    url = (
        f"{ESPN_BASE}/"
        f"{league_slug}/"
        f"teams/"
        f"{team_id}/"
        f"schedule"
    )

    data = espn_get(
        url,
        cache_key=(
            f"schedule:"
            f"{league_slug}:"
            f"{team_id}"
        )
    )

    eventi = estrai_eventi(data)

    risultati = []

    limite = oggi_utc() - timedelta(
        days=giorni
    )

    for evento in eventi:

        info = crea_info_evento(evento)

        if not info:
            continue

        dt = parse_data(info["date"])

        if not dt:
            continue

        if dt < limite:
            continue

        risultati.append(info)

    risultati.sort(
        key=lambda x: (
            parse_data(x["date"])
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True
    )

    return risultati


# ============================================================
# FORMA
# ============================================================

def partita_di_squadra(
    partita,
    team_id
):

    team_id = str(team_id)

    if str(partita["home_id"]) == team_id:
        return {
            "gf": partita["home_score"],
            "gs": partita["away_score"],
            "casa": True,
            "data": partita["date"],
            "vinta": (
                partita["home_score"]
                > partita["away_score"]
            ),
            "pareggiata": (
                partita["home_score"]
                == partita["away_score"]
            )
        }

    if str(partita["away_id"]) == team_id:
        return {
            "gf": partita["away_score"],
            "gs": partita["home_score"],
            "casa": False,
            "data": partita["date"],
            "vinta": (
                partita["away_score"]
                > partita["home_score"]
            ),
            "pareggiata": (
                partita["away_score"]
                == partita["home_score"]
            )
        }

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

    risultati = []

    for partita in calendario:

        if not partita["completed"]:
            continue

        risultato = partita_di_squadra(
            partita,
            team_id
        )

        if risultato:
            risultati.append(risultato)

        if len(risultati) >= numero:
            break

    return risultati


def calcola_statistiche_form(form):

    if not form:
        return {
            "partite": 0,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0,
            "gf": 0,
            "gs": 0,
            "media_gf": 0,
            "media_gs": 0,
            "punti": 0,
            "ppp": 0,
            "trend": "N/D"
        }

    vittorie = sum(
        1 for x in form
        if x["vinta"]
    )

    pareggi = sum(
        1 for x in form
        if x["pareggiata"]
    )

    sconfitte = (
        len(form)
        - vittorie
        - pareggi
    )

    gf = sum(
        safe_int(x["gf"])
        for x in form
    )

    gs = sum(
        safe_int(x["gs"])
        for x in form
    )

    punti = (
        vittorie * 3
        + pareggi
    )

    partite = len(form)

    ppp = (
        punti / partite
        if partite
        else 0
    )

    trend = "".join(
        "V" if x["vinta"]
        else "X" if x["pareggiata"]
        else "P"
        for x in reversed(form)
    )

    return {
        "partite": partite,
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte,
        "gf": gf,
        "gs": gs,
        "media_gf": gf / partite,
        "media_gs": gs / partite,
        "punti": punti,
        "ppp": ppp,
        "trend": trend
    }


def calcola_split(
    form,
    casa
):

    split = [
        x for x in form
        if x["casa"] == casa
    ]

    return calcola_statistiche_form(
        split
    )


# ============================================================
# CLASSIFICA
# ============================================================

def estrai_stat(team):

    if not team:
        return {}

    stats = team.get(
        "stats",
        []
    )

    risultato = {}

    for stat in stats:

        nome = stat.get("name")

        valore = stat.get(
            "value"
        )

        if nome:
            risultato[nome] = (
                safe_float(valore)
            )

    return risultato


def recupera_classifica(
    league_slug
):

    url = (
        f"{ESPN_STANDINGS_BASE}/"
        f"{league_slug}/"
        f"standings"
    )

    data = espn_get(
        url,
        cache_key=(
            f"standings:"
            f"{league_slug}"
        )
    )

    return data


def trova_classifica_squadra(
    data,
    team_id,
    nome_squadra=""
):

    if not data:
        return {}

    team_id = str(team_id)

    def cerca(obj):

        if isinstance(obj, dict):

            team = obj.get("team")

            if isinstance(team, dict):

                tid = str(
                    team.get("id", "")
                )

                tname = (
                    team.get("displayName")
                    or team.get("name")
                    or ""
                )

                if (
                    tid == team_id
                    or (
                        nome_squadra
                        and normalizza_nome(
                            tname
                        )
                        == normalizza_nome(
                            nome_squadra
                        )
                    )
                ):
                    return obj

            for valore in obj.values():

                trovato = cerca(valore)

                if trovato:
                    return trovato

        elif isinstance(obj, list):

            for valore in obj:

                trovato = cerca(valore)

                if trovato:
                    return trovato

        return None

    risultato = cerca(data)

    if not risultato:
        return {}

    return {
        "rank": safe_int(
            risultato.get("rank")
        ),
        "team": risultato.get(
            "team",
            {}
        ),
        "stats": estrai_stat(
            risultato
        )
    }


# ============================================================
# INFORTUNI
# ============================================================

def recupera_infortuni(
    league_slug,
    team_id
):

    if not team_id:
        return []

    url = (
        f"{ESPN_BASE}/"
        f"{league_slug}/"
        f"teams/"
        f"{team_id}/"
        f"injuries"
    )

    data = espn_get(
        url,
        cache_key=(
            f"injuries:"
            f"{league_slug}:"
            f"{team_id}"
        )
    )

    if isinstance(
        data.get("injuries"),
        list
    ):
        return data["injuries"]

    return []


def analizza_infortuni(
    injuries
):

    importanti = 0

    for item in injuries:

        status = str(
            item.get(
                "status",
                ""
            )
        ).lower()

        if any(
            parola in status
            for parola in (
                "out",
                "injured",
                "doubt"
            )
        ):
            importanti += 1

    return {
        "totale": len(injuries),
        "importanti": importanti
    }


# ============================================================
# HEAD TO HEAD
# ============================================================

def recupera_h2h(
    league_slug,
    home_id,
    away_id
):

    if not home_id or not away_id:
        return []

    calendario = recupera_calendario_squadra(
        league_slug,
        home_id,
        giorni=1000
    )

    h2h = []

    for partita in calendario:

        ids = {
            str(partita["home_id"]),
            str(partita["away_id"])
        }

        if (
            str(home_id) in ids
            and str(away_id) in ids
            and partita["completed"]
        ):
            h2h.append(partita)

    h2h.sort(
        key=lambda x: (
            parse_data(x["date"])
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True
    )

    return h2h[:5]


def calcola_h2h(
    h2h,
    home_id
):

    risultato = {
        "partite": 0,
        "home_vittorie": 0,
        "pareggi": 0,
        "away_vittorie": 0,
        "gol_home": 0,
        "gol_away": 0
    }

    for partita in h2h:

        risultato["partite"] += 1

        if (
            str(partita["home_id"])
            == str(home_id)
        ):
            gf = partita["home_score"]
            gs = partita["away_score"]

            risultato["gol_home"] += gf
            risultato["gol_away"] += gs

            if gf > gs:
                risultato["home_vittorie"] += 1
            elif gf == gs:
                risultato["pareggi"] += 1
            else:
                risultato["away_vittorie"] += 1

        else:
            gf = partita["away_score"]
            gs = partita["home_score"]

            risultato["gol_home"] += gf
            risultato["gol_away"] += gs

            if gf > gs:
                risultato["away_vittorie"] += 1
            elif gf == gs:
                risultato["pareggi"] += 1
            else:
                risultato["home_vittorie"] += 1

    return risultato


# ============================================================
# COMPETIZIONI EUROPEE E FATICA
# ============================================================

def recupera_eventi_europei_squadra(
    team_id,
    giorni=30
):

    if not team_id:
        return []

    risultati = []

    for slug in COMPETIZIONI_EUROPEE:

        url = (
            f"{ESPN_BASE}/"
            f"{slug}/"
            f"scoreboard"
        )

        data = espn_get(
            url,
            cache_key=(
                f"europe:"
                f"{slug}"
            )
        )

        for evento in estrai_eventi(data):

            home, away = (
                estrai_squadre_evento(
                    evento
                )
            )

            if not home or not away:
                continue

            ids = {
                id_squadra_competitor(
                    home
                ),
                id_squadra_competitor(
                    away
                )
            }

            if str(team_id) not in ids:
                continue

            info = crea_info_evento(
                evento
            )

            if info:
                risultati.append(
                    info
                )

    limite = oggi_utc() - timedelta(
        days=giorni
    )

    return [
        x for x in risultati
        if (
            parse_data(x["date"])
            and parse_data(x["date"]) >= limite
        )
    ]


def analizza_fatica(
    league_slug,
    team_id
):

    calendario = recupera_calendario_squadra(
        league_slug,
        team_id,
        giorni=30
    )

    completate = [
        x for x in calendario
        if x["completed"]
    ]

    europee = recupera_eventi_europei_squadra(
        team_id,
        giorni=30
    )

    adesso = oggi_utc()

    ultime = []

    for partita in completate:

        dt = parse_data(
            partita["date"]
        )

        if dt:
            giorni = (
                adesso - dt
            ).total_seconds() / 86400

            ultime.append(giorni)

    ultima = (
        min(ultime)
        if ultime
        else 999
    )

    partite_14 = 0

    limite = adesso - timedelta(
        days=14
    )

    for partita in completate:

        dt = parse_data(
            partita["date"]
        )

        if dt and dt >= limite:
            partite_14 += 1

    europee_recenti = 0

    for partita in europee:

        dt = parse_data(
            partita["date"]
        )

        if dt and dt >= limite:
            europee_recenti += 1

    if ultima < 3:
        livello = "ALTA"
    elif ultima < 5:
        livello = "MEDIA"
    else:
        livello = "BASSA"

    if partite_14 >= 6:
        livello = "MOLTO ALTA"

    return {
        "giorni_riposo": round(
            ultima,
            1
        ),
        "partite_14_giorni": partite_14,
        "europee_recenti": europee_recenti,
        "livello": livello
    }


# ============================================================
# MOMENTUM
# ============================================================

def analizza_momento(
    statistiche
):

    ppp = statistiche.get(
        "ppp",
        0
    )

    vittorie = statistiche.get(
        "vittorie",
        0
    )

    sconfitte = statistiche.get(
        "sconfitte",
        0
    )

    if ppp >= 2.2:
        livello = "MOLTO POSITIVO"

    elif ppp >= 1.6:
        livello = "POSITIVO"

    elif ppp >= 1.0:
        livello = "NEUTRO"

    elif ppp >= 0.7:
        livello = "NEGATIVO"

    else:
        livello = "MOLTO NEGATIVO"

    return {
        "livello": livello,
        "ppp": ppp,
        "vittorie": vittorie,
        "sconfitte": sconfitte
    }


# ============================================================
# PROBABILITÀ
# ============================================================

def probabilita_da_forze(
    forza_home,
    forza_away
):

    totale = (
        forza_home
        + forza_away
    )

    if totale <= 0:
        return 33.3, 33.3, 33.4

    base_home = (
        forza_home / totale
    )

    base_away = (
        forza_away / totale
    )

    pareggio = 0.27

    p_home = (
        base_home
        * (1 - pareggio)
    )

    p_away = (
        base_away
        * (1 - pareggio)
    )

    return (
        p_home * 100,
        pareggio * 100,
        p_away * 100
    )


def calcola_goal_probabilita(
    home_stats,
    away_stats,
    home_split,
    away_split
):

    home_gf = (
        home_stats["media_gf"]
        + home_split["media_gf"]
    ) / 2

    away_gf = (
        away_stats["media_gf"]
        + away_split["media_gf"]
    ) / 2

    home_gs = (
        home_stats["media_gs"]
        + home_split["media_gs"]
    ) / 2

    away_gs = (
        away_stats["media_gs"]
        + away_split["media_gs"]
    ) / 2

    expected_home = (
        home_gf * 0.60
        + away_gs * 0.40
    )

    expected_away = (
        away_gf * 0.60
        + home_gs * 0.40
    )

    expected_total = (
        expected_home
        + expected_away
    )

    # Stima prudente delle probabilità
    over15 = min(
        95,
        max(
            25,
            45 + expected_total * 18
        )
    )

    over25 = min(
        90,
        max(
            15,
            25 + expected_total * 17
        )
    )

    btts = min(
        90,
        max(
            15,
            20
            + min(
                expected_home,
                expected_away
            ) * 30
        )
    )

    return {
        "expected_home": expected_home,
        "expected_away": expected_away,
        "expected_total": expected_total,
        "over15": over15,
        "over25": over25,
        "btts": btts
    }


# ============================================================
# ANALISI COMPLETA PARTITA
# ============================================================

def analizza_partita(
    partita,
    league_slug
):

    home = partita["home"]
    away = partita["away"]

    home_id = partita["home_id"]
    away_id = partita["away_id"]

    print(
        f"   📊 Raccolta dati: {home} "
        f"vs {away}"
    )

    # --------------------------------------------------------
    # FORMA
    # --------------------------------------------------------

    home_form = recupera_form_squadra(
        league_slug,
        home_id
    )

    away_form = recupera_form_squadra(
        league_slug,
        away_id
    )

    home_stats = calcola_statistiche_form(
        home_form
    )

    away_stats = calcola_statistiche_form(
        away_form
    )

    print(
        f"   📈 Forma: "
        f"{home_stats['trend']} - "
        f"{away_stats['trend']}"
    )

    # --------------------------------------------------------
    # CASA / TRASFERTA
    # --------------------------------------------------------

    home_split = calcola_split(
        home_form,
        True
    )

    away_split = calcola_split(
        away_form,
        False
    )

    # --------------------------------------------------------
    # CLASSIFICA
    # --------------------------------------------------------

    standings = recupera_classifica(
        league_slug
    )

    home_table = trova_classifica_squadra(
        standings,
        home_id,
        home
    )

    away_table = trova_classifica_squadra(
        standings,
        away_id,
        away
    )

    home_rank = home_table.get(
        "rank",
        0
    )

    away_rank = away_table.get(
        "rank",
        0
    )

    # --------------------------------------------------------
    # INFORTUNI
    # --------------------------------------------------------

    home_injuries = analizza_infortuni(
        recupera_infortuni(
            league_slug,
            home_id
        )
    )

    away_injuries = analizza_infortuni(
        recupera_infortuni(
            league_slug,
            away_id
        )
    )

    # --------------------------------------------------------
    # FATICA
    # --------------------------------------------------------

    home_fatigue = analizza_fatica(
        league_slug,
        home_id
    )

    away_fatigue = analizza_fatica(
        league_slug,
        away_id
    )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h = recupera_h2h(
        league_slug,
        home_id,
        away_id
    )

    h2h_stats = calcola_h2h(
        h2h,
        home_id
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    home_momentum = analizza_momento(
        home_stats
    )

    away_momentum = analizza_momento(
        away_stats
    )

    # --------------------------------------------------------
    # FORZA SQUADRE
    # --------------------------------------------------------

    forza_home = 1.0
    forza_away = 1.0

    # Forma
    forza_home += (
        home_stats["ppp"] - 1
    ) * 0.35

    forza_away += (
        away_stats["ppp"] - 1
    ) * 0.35

    # Casa
    forza_home += 0.18

    # Gol
    forza_home += (
        home_stats["media_gf"]
        - home_stats["media_gs"]
    ) * 0.12

    forza_away += (
        away_stats["media_gf"]
        - away_stats["media_gs"]
    ) * 0.12

    # Classifica
    if home_rank and away_rank:

        differenza_classifica = (
            away_rank
            - home_rank
        )

        forza_home += (
            differenza_classifica
            * 0.015
        )

        forza_away -= (
            differenza_classifica
            * 0.015
        )

    # Infortuni
    forza_home -= (
        home_injuries["importanti"]
        * 0.025
    )

    forza_away -= (
        away_injuries["importanti"]
        * 0.025
    )

    # Fatica
    if home_fatigue["livello"] in (
        "ALTA",
        "MOLTO ALTA"
    ):
        forza_home -= 0.07

    if away_fatigue["livello"] in (
        "ALTA",
        "MOLTO ALTA"
    ):
        forza_away -= 0.07

    forza_home = max(
        0.10,
        forza_home
    )

    forza_away = max(
        0.10,
        forza_away
    )

    # --------------------------------------------------------
    # 1X2
    # --------------------------------------------------------

    p_home, p_draw, p_away = (
        probabilita_da_forze(
            forza_home,
            forza_away
        )
    )

    # --------------------------------------------------------
    # GOAL
    # --------------------------------------------------------

    goal = calcola_goal_probabilita(
        home_stats,
        away_stats,
        home_split,
        away_split
    )

    # --------------------------------------------------------
    # H2H CORREZIONE
    # --------------------------------------------------------

    if h2h_stats["partite"] >= 3:

        if (
            h2h_stats["home_vittorie"]
            >= h2h_stats["away_vittorie"]
            + 2
        ):
            p_home += 3
            p_away -= 2
            p_draw -= 1

        elif (
            h2h_stats["away_vittorie"]
            >= h2h_stats["home_vittorie"]
            + 2
        ):
            p_away += 3
            p_home -= 2
            p_draw -= 1

    # normalizzazione
    totale = (
        p_home
        + p_draw
        + p_away
    )

    if totale > 0:

        p_home = (
            p_home / totale * 100
        )

        p_draw = (
            p_draw / totale * 100
        )

        p_away = (
            p_away / totale * 100
        )

    # --------------------------------------------------------
    # PRONOSTICO PRINCIPALE
    # --------------------------------------------------------

    probabilita = {
        "1": p_home,
        "X": p_draw,
        "2": p_away
    }

    segno = max(
        probabilita,
        key=probabilita.get
    )

    prob_segno = probabilita[
        segno
    ]

    # Doppia chance
    if p_home >= p_away:
        doppia_chance = "1X"
        prob_dc = p_home + p_draw
    else:
        doppia_chance = "X2"
        prob_dc = p_away + p_draw

    # Over / Under
    if goal["over25"] >= 62:
        over_under = "Over 2.5"
        prob_ou = goal["over25"]
    else:
        over_under = "Under 3.5"
        prob_ou = 100 - min(
            80,
            goal["over25"] * 0.75
        )

    # BTTS
    if goal["btts"] >= 60:
        btts = "Goal"
        prob_btts = goal["btts"]
    else:
        btts = "No Goal"
        prob_btts = 100 - goal["btts"]

    # --------------------------------------------------------
    # PUNTEGGIO AFFIDABILITÀ
    # --------------------------------------------------------

    fattori = []

    fattori.append(
        min(
            100,
            max(
                0,
                prob_segno
            )
        )
    )

    fattori.append(
        min(
            100,
            max(
                0,
                prob_dc
            )
        )
    )

    fattori.append(
        min(
            100,
            max(
                0,
                prob_ou
            )
        )
    )

    fattori.append(
        min(
            100,
            max(
                0,
                prob_btts
            )
        )
    )

    affidabilita = (
        sum(fattori)
        / len(fattori)
    )

    # Penalità per pochi dati
    if (
        home_stats["partite"] < 5
        or away_stats["partite"] < 5
    ):
        affidabilita -= 5

    # Penalità per forte fatica
    if (
        home_fatigue["livello"]
        == "MOLTO ALTA"
        or
        away_fatigue["livello"]
        == "MOLTO ALTA"
    ):
        affidabilita -= 3

    affidabilita = min(
        95,
        max(
            50,
            affidabilita
        )
    )

    return {
        "segno": segno,
        "prob_1": p_home,
        "prob_x": p_draw,
        "prob_2": p_away,

        "doppia_chance": doppia_chance,
        "prob_dc": prob_dc,

        "over_under": over_under,
        "prob_ou": prob_ou,

        "btts": btts,
        "prob_btts": prob_btts,

        "affidabilita": affidabilita,

        "expected_home": goal[
            "expected_home"
        ],
        "expected_away": goal[
            "expected_away"
        ],

        "home_stats": home_stats,
        "away_stats": away_stats,

        "home_split": home_split,
        "away_split": away_split,

        "home_rank": home_rank,
        "away_rank": away_rank,

        "home_injuries": home_injuries,
        "away_injuries": away_injuries,

        "home_fatigue": home_fatigue,
        "away_fatigue": away_fatigue,

        "home_momentum": home_momentum,
        "away_momentum": away_momentum,

        "h2h": h2h_stats
    }


# ============================================================
# FORMATTAZIONE PRONOSTICO
# ============================================================

def formatta_pronostico(
    partita,
    pronostico
):

    home = partita["home"]
    away = partita["away"]

    data = format_data_italiana(
        partita["date"]
    )

    segno = pronostico["segno"]

    if segno == "1":
        esito = (
            f"1 — {pronostico['prob_1']:.0f}%"
        )
    elif segno == "2":
        esito = (
            f"2 — {pronostico['prob_2']:.0f}%"
        )
    else:
        esito = (
            f"X — {pronostico['prob_x']:.0f}%"
        )

    hs = pronostico["home_stats"]
    aws = pronostico["away_stats"]

    hf = pronostico["home_fatigue"]
    af = pronostico["away_fatigue"]

    hm = pronostico["home_momentum"]
    am = pronostico["away_momentum"]

    hi = pronostico["home_injuries"]
    ai = pronostico["away_injuries"]

    testo = (
        f"⚽ <b>{home} - {away}</b>\n"
        f"📅 {data}\n\n"

        f"🎯 <b>PRONOSTICO PRINCIPALE</b>\n"
        f"➡️ {esito}\n"
        f"➡️ Doppia chance: "
        f"<b>{pronostico['doppia_chance']}</b> "
        f"({pronostico['prob_dc']:.0f}%)\n"
        f"➡️ {pronostico['over_under']} "
        f"({pronostico['prob_ou']:.0f}%)\n"
        f"➡️ {pronostico['btts']} "
        f"({pronostico['prob_btts']:.0f}%)\n\n"

        f"⭐ <b>Affidabilità stimata: "
        f"{pronostico['affidabilita']:.0f}%</b>\n\n"

        f"📊 <b>FORMA</b>\n"
        f"{home}: {hs['trend']} "
        f"({hs['ppp']:.2f} punti/partita)\n"
        f"{away}: {aws['trend']} "
        f"({aws['ppp']:.2f} punti/partita)\n\n"

        f"⚽ <b>GOL</b>\n"
        f"{home}: "
        f"{hs['media_gf']:.2f} fatti / "
        f"{hs['media_gs']:.2f} subiti\n"
        f"{away}: "
        f"{aws['media_gf']:.2f} fatti / "
        f"{aws['media_gs']:.2f} subiti\n"
        f"📈 Gol attesi: "
        f"{pronostico['expected_home']:.2f} - "
        f"{pronostico['expected_away']:.2f}\n\n"

        f"🏠 <b>CASA / TRASFERTA</b>\n"
        f"{home} in casa: "
        f"{pronostico['home_split']['ppp']:.2f} "
        f"punti/partita\n"
        f"{away} fuori casa: "
        f"{pronostico['away_split']['ppp']:.2f} "
        f"punti/partita\n\n"

        f"📋 <b>CLASSIFICA</b>\n"
        f"{home}: "
        f"{pronostico['home_rank'] or 'N/D'}°\n"
        f"{away}: "
        f"{pronostico['away_rank'] or 'N/D'}°\n\n"

        f"🔥 <b>MOMENTUM</b>\n"
        f"{home}: {hm['livello']}\n"
        f"{away}: {am['livello']}\n\n"

        f"🩹 <b>INFORTUNI DISPONIBILI</b>\n"
        f"{home}: {hi['importanti']} rilevanti\n"
        f"{away}: {ai['importanti']} rilevanti\n\n"

        f"🛌 <b>FATICA / RIPOSO</b>\n"
        f"{home}: {hf['giorni_riposo']:.1f} giorni "
        f"di riposo — {hf['livello']}\n"
        f"{away}: {af['giorni_riposo']:.1f} giorni "
        f"di riposo — {af['livello']}\n"
    )

    h2h = pronostico["h2h"]

    if h2h["partite"] > 0:

        testo += (
            f"\n🤝 <b>HEAD TO HEAD</b>\n"
            f"Ultimi confronti disponibili: "
            f"{h2h['partite']}\n"
            f"Vittorie {home}: "
            f"{h2h['home_vittorie']}\n"
            f"Pareggi: "
            f"{h2h['pareggi']}\n"
            f"Vittorie {away}: "
            f"{h2h['away_vittorie']}\n"
        )

    return testo


# ============================================================
# CREAZIONE REPORT
# ============================================================

def crea_report(
    nome_campionato,
    league_slug
):

    print("=" * 42)
    print(
        f"🚨 CREAREPORT: {nome_campionato}"
    )
    print("=" * 42)

    print(
        f"🔎 Recupero partite future "
        f"di {nome_campionato}..."
    )

    partite = recupera_partite_future(
        league_slug
    )

    print(
        f"📋 Partite trovate: "
        f"{len(partite)}"
    )

    if not partite:

        print(
            "⚠️ Nessuna partita futura "
            "trovata."
        )

        return (
            f"⚠️ Al momento non risultano "
            f"partite future disponibili "
            f"per {nome_campionato}."
        )

    # --------------------------------------------------------
    # Seleziona le prime partite
    # --------------------------------------------------------

    partite = partite[
        :NUM_PARTITE_REPORT
    ]

    print(
        f"🎯 Partite da analizzare: "
        f"{len(partite)}"
    )

    risultati = []

    for indice, partita in enumerate(
        partite
    ):

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

                print(
                    f"⚠️ Nessun pronostico "
                    f"generato per "
                    f"{partita['home']} - "
                    f"{partita['away']}"
                )

                continue

            testo = formatta_pronostico(
                partita,
                pronostico
            )

            risultati.append(
                (
                    pronostico[
                        "affidabilita"
                    ],
                    testo
                )
            )

            print(
                f"✅ Pronostico creato: "
                f"{partita['home']} - "
                f"{partita['away']} | "
                f"Affidabilità "
                f"{pronostico['affidabilita']:.0f}%"
            )

        except Exception as exc:

            print(
                f"❌ Errore analisi "
                f"{partita['home']} - "
                f"{partita['away']}: "
                f"{exc}"
            )

            traceback.print_exc()

    # ========================================================
    # QUESTE SONO LE RIGHE CHE MANCAVANO NEI LOG
    # ========================================================

    print("=" * 42)
    print(
        f"🏁 ANALISI COMPLETATE: "
        f"{len(partite)}/{len(partite)}"
    )

    print(
        f"📊 Pronostici validi creati: "
        f"{len(risultati)}"
    )

    print("=" * 42)

    if not risultati:

        print(
            "⚠️ Nessun pronostico valido "
            "disponibile."
        )

        return (
            f"⚠️ Non è stato possibile "
            f"generare pronostici validi "
            f"per {nome_campionato}."
        )

    # migliori pronostici prima
    risultati.sort(
        key=lambda x: x[0],
        reverse=True
    )

    blocchi = [
        testo
        for _, testo in risultati
    ]

    intestazione = (
        f"🚨 <b>PRONOSTICI MIGLIORI "
        f"{nome_campionato.upper()}</b>\n\n"
        f"📊 Analizzate "
        f"<b>{len(partite)}</b> partite\n"
        f"⭐ Selezionati "
        f"<b>{len(blocchi)}</b> pronostici\n\n"
    )

    report = (
        intestazione
        + "\n\n".join(blocchi)
    )

    print(
        f"📨 Report finale creato."
    )

    print(
        f"📏 Lunghezza report: "
        f"{len(report)} caratteri"
    )

    print(
        "✅ CREA_REPORT TERMINATO "
        "CORRETTAMENTE."
    )

    print("=" * 42)

    return report


# ============================================================
# INVIO REPORT TELEGRAM
# ============================================================

def invia_report_diviso(
    chat_id,
    testo
):

    limite = 3900

    print("=" * 42)
    print(
        f"📨 INVIO REPORT TELEGRAM"
    )
    print(
        f"📏 Lunghezza totale: "
        f"{len(testo)} caratteri"
    )

    if len(testo) <= limite:

        print(
            "📨 Report contenuto "
            "in un solo messaggio."
        )

        bot.send_message(
            chat_id,
            testo
        )

        print(
            "✅ Messaggio Telegram inviato."
        )

        print(
            "🏁 REPORT TELEGRAM "
            "INVIATO COMPLETAMENTE."
        )

        print("=" * 42)

        return

    # --------------------------------------------------------
    # Divisione report
    # --------------------------------------------------------

    parti = []

    blocchi = testo.split(
        "\n\n"
    )

    corrente = ""

    for blocco in blocchi:

        if (
            len(corrente)
            + len(blocco)
            + 2
            <= limite
        ):

            if corrente:
                corrente += "\n\n"

            corrente += blocco

        else:

            if corrente:
                parti.append(
                    corrente
                )

            # sicurezza per blocchi
            # singolarmente troppo lunghi
            if len(blocco) <= limite:

                corrente = blocco

            else:

                start = 0

                while start < len(blocco):

                    fine = min(
                        start + limite,
                        len(blocco)
                    )

                    parti.append(
                        blocco[start:fine]
                    )

                    start = fine

                corrente = ""

    if corrente:
        parti.append(
            corrente
        )

    print(
        f"📨 Messaggi da inviare: "
        f"{len(parti)}"
    )

    # --------------------------------------------------------
    # Invio
    # --------------------------------------------------------

    for indice, parte in enumerate(
        parti,
        1
    ):

        print(
            f"📤 Invio messaggio "
            f"{indice}/{len(parti)} "
            f"({len(parte)} caratteri)"
        )

        try:

            bot.send_message(
                chat_id,
                parte
            )

            print(
                f"✅ Messaggio "
                f"{indice}/{len(parti)} "
                f"inviato."
            )

        except Exception as exc:

            print(
                f"❌ Errore invio "
                f"messaggio "
                f"{indice}/{len(parti)}: "
                f"{exc}"
            )

            traceback.print_exc()

            raise

        time.sleep(0.3)

    print(
        "🏁 REPORT TELEGRAM "
        "INVIATO COMPLETAMENTE."
    )

    print("=" * 42)


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def start(message):

    print(
        f"📩 /start ricevuto "
        f"da chat {message.chat.id}"
    )

    testo = (
        "⚽ <b>BOT PRONOSTICI CALCIO</b>\n\n"
        "Benvenuto!\n\n"
        "Posso analizzare i principali "
        "campionati europei e generare "
        "i pronostici migliori.\n\n"
        "📊 Scrivi il nome del campionato "
        "oppure usa /campionati"
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(
    commands=["help"]
)
def help_command(message):

    print(
        f"📩 /help ricevuto "
        f"da chat {message.chat.id}"
    )

    testo = (
        "ℹ️ <b>COME USARE IL BOT</b>\n\n"
        "Scrivi ad esempio:\n"
        "• Serie A\n"
        "• Premier League\n"
        "• La Liga\n"
        "• Bundesliga\n"
        "• Ligue 1\n\n"
        "Il bot analizzerà le partite "
        "future disponibili."
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# /CAMPIONATI
# ============================================================

@bot.message_handler(
    commands=["campionati"]
)
def campionati(message):

    print(
        f"📩 /campionati ricevuto "
        f"da chat {message.chat.id}"
    )

    testo = (
        "🏆 <b>CAMPIONATI DISPONIBILI</b>\n\n"
    )

    for nome in CAMPIONATI:
        testo += f"{nome}\n"

    testo += (
        "\n✍️ Scrivi il nome del "
        "campionato che vuoi analizzare."
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# TROVA CAMPIONATO
# ============================================================

def trova_campionato(testo):

    testo_norm = normalizza_nome(
        testo
    )

    for nome, config in CAMPIONATI.items():

        nome_norm = normalizza_nome(
            nome
        )

        nome_config = normalizza_nome(
            config["nome"]
        )

        if (
            testo_norm == nome_norm
            or testo_norm == nome_config
        ):
            return config

    # abbreviazioni
    if testo_norm in (
        "seriea",
        "serie a"
    ):
        return CAMPIONATI[
            "🇮🇹 Serie A"
        ]

    if testo_norm in (
        "premier",
        "premier league"
    ):
        return CAMPIONATI[
            "🏴 Premier League"
        ]

    if testo_norm in (
        "liga",
        "la liga"
    ):
        return CAMPIONATI[
            "🇪🇸 La Liga"
        ]

    if testo_norm in (
        "bundesliga",
        "bundes"
    ):
        return CAMPIONATI[
            "🇩🇪 Bundesliga"
        ]

    if testo_norm in (
        "ligue1",
        "ligue 1"
    ):
        return CAMPIONATI[
            "🇫🇷 Ligue 1"
        ]

    return None


# ============================================================
# MESSAGGI TESTUALI
# ============================================================

@bot.message_handler(
    func=lambda message: True,
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

        print(
            f"⚠️ Testo non riconosciuto: "
            f"{testo}"
        )

        bot.send_message(
            message.chat.id,
            "⚠️ Campionato non riconosciuto.\n\n"
            "Usa /campionati per vedere "
            "quelli disponibili."
        )

        return

    config = campionato

    print("=" * 42)
    print(
        f"📨 RICHIESTA CAMPIONATO: "
        f"{config['nome']}"
    )

    print(
        f"👤 Chat ID: "
        f"{message.chat.id}"
    )

    bot.send_message(
        message.chat.id,
        f"🔎 Analizzo <b>{config['nome']}</b>...\n"
        f"⏳ Sto confrontando forma, "
        f"gol, classifica, casa/trasferta, "
        f"fatica, infortuni e H2H."
    )

    try:

        report = crea_report(
            config["nome"],
            config["espn"]
        )

        print(
            "📊 Report generato."
        )

        print(
            "📤 Avvio invio Telegram..."
        )

        invia_report_diviso(
            message.chat.id,
            report
        )

        print(
            f"✅ ELABORAZIONE "
            f"{config['nome']} TERMINATA."
        )

        print("=" * 42)

    except Exception as exc:

        print(
            f"❌ ERRORE CREAZIONE REPORT: "
            f"{exc}"
        )

        traceback.print_exc()

        try:

            bot.send_message(
                message.chat.id,
                "❌ Si è verificato un "
                "errore durante l'analisi. "
                "Controlla i log di Render."
            )

        except Exception as telegram_error:

            print(
                f"❌ Errore invio messaggio "
                f"di errore Telegram: "
                f"{telegram_error}"
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
        # Evita log HTTP inutilmente rumorosi
        return

    def do_GET(self):

        if self.path in (
            "/",
            "/health"
        ):

            risposta = (
                "Bot pronostici calcio "
                "attivo."
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(
                    risposta.encode("utf-8")
                ))
            )

            self.end_headers()

            self.wfile.write(
                risposta.encode("utf-8")
            )

            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        print(
            f"📩 POST RICEVUTA: "
            f"{self.path}"
        )

        if self.path != WEBHOOK_PATH:

            print(
                "⚠️ POST su percorso "
                "non riconosciuto."
            )

            self.send_response(404)
            self.end_headers()

            return

        try:

            content_length = safe_int(
                self.headers.get(
                    "Content-Length"
                ),
                0
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

            print(
                "⚙️ Elaborazione update "
                "Telegram..."
            )

            update = (
                telebot.types.Update
                .de_json(update_data)
            )

            print(
                "✅ Update Telegram elaborato."
            )

            def process_update():

                try:

                    bot.process_new_updates(
                        [update]
                    )

                    print(
                        "✅ Update inviato "
                        "al thread."
                    )

                except Exception as exc:

                    print(
                        f"❌ Errore elaborazione "
                        f"update: {exc}"
                    )

                    traceback.print_exc()

            thread = threading.Thread(
                target=process_update,
                daemon=True
            )

            thread.start()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(
                b"OK"
            )

        except Exception as exc:

            print(
                f"❌ ERRORE WEBHOOK: "
                f"{exc}"
            )

            traceback.print_exc()

            self.send_response(500)
            self.end_headers()


# ============================================================
# CONFIGURAZIONE WEBHOOK
# ============================================================

def configura_webhook():

    print("=" * 42)
    print(
        "🌐 CONFIGURAZIONE WEBHOOK TELEGRAM"
    )

    print(
        f"🌐 URL webhook: "
        f"{WEBHOOK_URL}"
    )

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

    except Exception as exc:

        print(
            f"❌ ERRORE CONFIGURAZIONE "
            f"WEBHOOK: {exc}"
        )

        traceback.print_exc()

        raise

    print("=" * 42)


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "🚀 Avvio server HTTP Render..."
    )

    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    server_thread.start()

    print(
        "=========================================="
    )

    print(
        "Server Render attivo."
    )

    print(
        "=========================================="
    )

    print(
        f"🌐 Server HTTP avviato "
        f"sulla porta {PORT}"
    )

    print(
        f"🌐 Health URL: "
        f"{RENDER_EXTERNAL_URL}/health"
    )

    print(
        "=========================================="
    )

    configura_webhook()

    print(
        "=========================================="
    )

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
        "=========================================="
    )

    # Mantiene vivo il processo Render.
    # NON usare infinity_polling().
    try:

        while True:
            time.sleep(60)

    except KeyboardInterrupt:

        print(
            "🛑 Arresto applicazione..."
        )

        server.shutdown()


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":
    main()
