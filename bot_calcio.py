import os
import json
import time
import math
import threading
import http.server
import traceback
from datetime import datetime, timezone

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

HTTP_TIMEOUT = 15

CACHE_TTL = 30 * 60

GIORNI_FUTURI = 14

NUM_PARTITE_REPORT = 8

NUM_FORM = 10


# ============================================================
# ESPN
# ============================================================

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

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
# CONTROLLO VARIABILI
# ============================================================

print("=" * 42)
print("⚽ BOT PRONOSTICI CALCIO")
print("Avvio applicazione Render...")
print("=" * 42)

if TELEGRAM_BOT_TOKEN:
    print("TELEGRAM_BOT_TOKEN: OK")
else:
    print("TELEGRAM_BOT_TOKEN: MANCANTE")

if FOOTBALL_API_KEY:
    print("FOOTBALL_API_KEY: OK")
else:
    print("FOOTBALL_API_KEY: MANCANTE")

print("=" * 42)


# ============================================================
# BOT TELEGRAM
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN non configurato.")

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)


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

def safe_float(value, default=None):
    try:
        if value is None:
            return default

        return float(value)

    except Exception:
        return default


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def normalizza_nome(nome):
    if not nome:
        return ""

    nome = str(nome).lower().strip()

    sostituzioni = {
        "ac milan": "milan",
        "milan": "milan",
        "inter milan": "inter",
        "internazionale": "inter",
        "juventus fc": "juventus",
        "juventus": "juventus",
        "as roma": "roma",
        "roma": "roma",
        "ss lazio": "lazio",
        "lazio": "lazio",
        "ssc napoli": "napoli",
        "napoli": "napoli"
    }

    return sostituzioni.get(nome, nome)


def timestamp_evento(event):
    try:
        date_value = event.get("date")

        if not date_value:
            return None

        return datetime.fromisoformat(
            date_value.replace("Z", "+00:00")
        )

    except Exception:
        return None


def data_formattata(event):
    dt = timestamp_evento(event)

    if not dt:
        return "Data N/D"

    return dt.astimezone().strftime("%d/%m/%Y %H:%M")


def nome_squadra(competitor):
    team = competitor.get("team", {})

    return (
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or "N/D"
    )


def team_id(competitor):
    team = competitor.get("team", {})

    return str(team.get("id", ""))


# ============================================================
# HTTP ESPN
# ============================================================

def espn_get(path, params=None, cache_key=None):
    if cache_key:
        cached = cache_get(cache_key)

        if cached is not None:
            return cached

    url = ESPN_BASE + "/" + path.lstrip("/")

    try:
        response = requests.get(
            url,
            params=params or {},
            timeout=HTTP_TIMEOUT
        )

        if response.status_code != 200:
            print(
                f"⚠️ ESPN HTTP {response.status_code}: {url}"
            )
            return None

        data = response.json()

        if cache_key:
            cache_set(cache_key, data)

        return data

    except requests.RequestException as exc:
        print(f"⚠️ Errore HTTP ESPN: {exc}")
        return None

    except Exception as exc:
        print(f"⚠️ Errore ESPN: {exc}")
        return None


# ============================================================
# EVENTI
# ============================================================

def estrai_eventi(data):
    if not data:
        return []

    return data.get("events", [])


def trova_competitor(event):
    competitors = (
        event.get("competitions", [{}])[0]
        .get("competitors", [])
    )

    home = None
    away = None

    for competitor in competitors:

        if competitor.get("homeAway") == "home":
            home = competitor

        elif competitor.get("homeAway") == "away":
            away = competitor

    return home, away


def evento_terminato(event):
    try:
        competition = event.get("competitions", [{}])[0]
        status = competition.get("status", {})
        type_data = status.get("type", {})

        return type_data.get("completed") is True

    except Exception:
        return False


def risultato_evento(event):
    home, away = trova_competitor(event)

    if not home or not away:
        return None

    try:
        hg = safe_float(home.get("score"))
        ag = safe_float(away.get("score"))

        if hg is None or ag is None:
            return None

        return int(hg), int(ag)

    except Exception:
        return None


# ============================================================
# PARTITE FUTURE DEL CAMPIONATO
# ============================================================

def recupera_partite_future(campionato):
    config = CAMPIONATI.get(campionato)

    if not config:
        return []

    league = config["espn"]

    print(
        f"🔎 Recupero partite future di {config['nome']}..."
    )

    oggi = datetime.now(timezone.utc)

    eventi = []

    # Recuperiamo diverse giornate/intervalli.
    # Evitiamo una singola richiesta enorme.
    for offset in range(0, GIORNI_FUTURI, 7):

        data_inizio = (
            oggi.date()
        )

        url_path = f"{league}/scoreboard"

        params = {
            "limit": 100,
            "dates": data_inizio.strftime("%Y%m%d")
        }

        # La query date ESPN può restituire gli eventi del giorno.
        # Per avere un intervallo più ampio usiamo anche calendario.
        data = espn_get(
            url_path,
            params=params,
            cache_key=f"scoreboard_{league}_{data_inizio}"
        )

        eventi.extend(
            estrai_eventi(data)
        )

        break

    # Tentativo calendario più ampio
    calendar_path = f"{league}/scoreboard"

    for giorno in range(GIORNI_FUTURI):

        giorno_data = oggi.date()

        # Query giornaliera.
        from datetime import timedelta

        giorno_data = giorno_data + timedelta(days=giorno)

        params = {
            "limit": 100,
            "dates": giorno_data.strftime("%Y%m%d")
        }

        data = espn_get(
            calendar_path,
            params=params,
            cache_key=f"giornata_{league}_{giorno_data}"
        )

        if data:
            eventi.extend(
                estrai_eventi(data)
            )

    # Elimina duplicati
    unici = {}

    for event in eventi:

        event_id = str(
            event.get("id", "")
        )

        if event_id:
            unici[event_id] = event

    futuri = []

    for event in unici.values():

        dt = timestamp_evento(event)

        if not dt:
            continue

        if dt <= oggi:
            continue

        home, away = trova_competitor(event)

        if not home or not away:
            continue

        futuri.append(event)

    futuri.sort(
        key=lambda e: timestamp_evento(e)
        or datetime.max.replace(tzinfo=timezone.utc)
    )

    print(
        f"📋 Partite trovate: {len(futuri)}"
    )

    return futuri


# ============================================================
# CALENDARIO / FORM SQUADRA
# ============================================================

def recupera_calendario_squadra(team_id_value):
    if not team_id_value:
        return []

    path = f"/soccer"

    # ESPN espone il calendario squadra tramite endpoint
    # site.api.espn.com/apis/site/v2/sports/soccer/
    # <league>/teams/<id>/schedule
    #
    # Il campionato viene recuperato dal campo league della
    # partita quando disponibile. In alternativa tentiamo
    # più competizioni.

    return []


def recupera_form_da_eventi(team_name, league):
    """
    Recupera la forma della squadra usando il calendario
    del campionato.

    Cerca gli eventi recenti e seleziona quelli in cui compare
    la squadra.
    """

    chiave = f"form_{league}_{normalizza_nome(team_name)}"

    cached = cache_get(chiave)

    if cached is not None:
        return cached

    oggi = datetime.now(timezone.utc)

    risultati = []

    from datetime import timedelta

    # Cerchiamo gli ultimi 90 giorni, un giorno alla volta.
    # La cache evita richieste ripetute.
    for giorno in range(1, 91):

        giorno_data = oggi.date() - timedelta(days=giorno)

        params = {
            "limit": 100,
            "dates": giorno_data.strftime("%Y%m%d")
        }

        data = espn_get(
            f"{league}/scoreboard",
            params=params,
            cache_key=f"formday_{league}_{giorno_data}"
        )

        if not data:
            continue

        for event in estrai_eventi(data):

            if not evento_terminato(event):
                continue

            home, away = trova_competitor(event)

            if not home or not away:
                continue

            home_name = nome_squadra(home)
            away_name = nome_squadra(away)

            if normalizza_nome(home_name) != normalizza_nome(team_name) and \
               normalizza_nome(away_name) != normalizza_nome(team_name):
                continue

            risultato = risultato_evento(event)

            if not risultato:
                continue

            hg, ag = risultato

            is_home = (
                normalizza_nome(home_name)
                == normalizza_nome(team_name)
            )

            if is_home:
                gf = hg
                gs = ag
            else:
                gf = ag
                gs = hg

            if gf > gs:
                esito = "V"
                punti = 3

            elif gf == gs:
                esito = "P"
                punti = 1

            else:
                esito = "S"
                punti = 0

            risultati.append({
                "data": timestamp_evento(event),
                "gf": gf,
                "gs": gs,
                "esito": esito,
                "punti": punti,
                "casa": is_home,
                "avversario": (
                    away_name if is_home else home_name
                )
            })

    risultati.sort(
        key=lambda x: x["data"] or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True
    )

    risultati = risultati[:NUM_FORM]

    cache_set(chiave, risultati)

    return risultati


# ============================================================
# STATISTICHE FORMA
# ============================================================

def statistiche_form(form):
    if not form:
        return {
            "sequenza": "N/D",
            "ppg": None,
            "gf_media": None,
            "gs_media": None,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0
        }

    punti = sum(
        x["punti"] for x in form
    )

    gf = sum(
        x["gf"] for x in form
    )

    gs = sum(
        x["gs"] for x in form
    )

    vittorie = sum(
        1 for x in form
        if x["esito"] == "V"
    )

    pareggi = sum(
        1 for x in form
        if x["esito"] == "P"
    )

    sconfitte = sum(
        1 for x in form
        if x["esito"] == "S"
    )

    n = len(form)

    return {
        "sequenza": "".join(
            x["esito"] for x in form
        ),
        "ppg": punti / n,
        "gf_media": gf / n,
        "gs_media": gs / n,
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte
    }


def rendimento_casa(form):
    casa = [
        x for x in form
        if x["casa"]
    ]

    if not casa:
        return None

    punti = sum(
        x["punti"] for x in casa
    )

    return punti / len(casa)


def rendimento_trasferta(form):
    trasferta = [
        x for x in form
        if not x["casa"]
    ]

    if not trasferta:
        return None

    punti = sum(
        x["punti"] for x in trasferta
    )

    return punti / len(trasferta)


# ============================================================
# CLASSIFICA
# ============================================================

def recupera_classifica(league):
    data = espn_get(
        f"{league}/standings",
        params={},
        cache_key=f"standings_{league}"
    )

    return data


def estrai_classifica_team(data, team_name):
    if not data:
        return None

    def cerca(obj):

        if isinstance(obj, dict):

            team = obj.get("team")

            if isinstance(team, dict):

                nome = (
                    team.get("displayName")
                    or team.get("name")
                    or ""
                )

                if normalizza_nome(nome) == \
                   normalizza_nome(team_name):

                    return obj

            for value in obj.values():

                result = cerca(value)

                if result:
                    return result

        elif isinstance(obj, list):

            for value in obj:

                result = cerca(value)

                if result:
                    return result

        return None

    return cerca(data)


def valore_standing(entry, nome):
    if not entry:
        return None

    stats = entry.get("stats", [])

    for stat in stats:

        if stat.get("name") == nome:
            return safe_float(
                stat.get("value")
            )

    return None


# ============================================================
# H2H
# ============================================================

def recupera_h2h(home_name, away_name, league):
    """
    Ricerca gli scontri diretti tra le due squadre
    nel calendario recente del campionato.
    """

    chiave = (
        f"h2h_{league}_"
        f"{normalizza_nome(home_name)}_"
        f"{normalizza_nome(away_name)}"
    )

    cached = cache_get(chiave)

    if cached is not None:
        return cached

    from datetime import timedelta

    oggi = datetime.now(timezone.utc)

    incontri = []

    for giorno in range(1, 730):

        giorno_data = oggi.date() - timedelta(days=giorno)

        params = {
            "limit": 100,
            "dates": giorno_data.strftime("%Y%m%d")
        }

        data = espn_get(
            f"{league}/scoreboard",
            params=params,
            cache_key=f"h2hday_{league}_{giorno_data}"
        )

        if not data:
            continue

        for event in estrai_eventi(data):

            if not evento_terminato(event):
                continue

            home, away = trova_competitor(event)

            if not home or not away:
                continue

            hn = nome_squadra(home)
            an = nome_squadra(away)

            if {
                normalizza_nome(hn),
                normalizza_nome(an)
            } == {
                normalizza_nome(home_name),
                normalizza_nome(away_name)
            }:

                risultato = risultato_evento(event)

                if risultato:
                    hg, ag = risultato

                    incontri.append({
                        "data": timestamp_evento(event),
                        "home": hn,
                        "away": an,
                        "hg": hg,
                        "ag": ag
                    })

    incontri.sort(
        key=lambda x: x["data"] or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True
    )

    incontri = incontri[:5]

    cache_set(chiave, incontri)

    return incontri


# ============================================================
# FATICA / RIPOSO
# ============================================================

def giorni_dall_ultima_partita(form):
    if not form:
        return None

    ultima = form[0].get("data")

    if not ultima:
        return None

    ora = datetime.now(timezone.utc)

    differenza = ora - ultima

    return max(
        0,
        differenza.total_seconds() / 86400
    )


def indice_fatica(form):
    giorni = giorni_dall_ultima_partita(form)

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
# IMPEGNI EUROPEI
# ============================================================

def verifica_impegni_europei(team_name):
    """
    Controllo indicativo degli impegni europei recenti.

    Non inventa dati: se ESPN non restituisce informazioni,
    il valore resta N/D.
    """

    trovato = []

    oggi = datetime.now(timezone.utc)

    from datetime import timedelta

    for league, nome_competizione in COMPETIZIONI_EUROPEE.items():

        for giorno in range(1, 22):

            giorno_data = (
                oggi.date() -
                timedelta(days=giorno)
            )

            params = {
                "limit": 100,
                "dates": giorno_data.strftime("%Y%m%d")
            }

            data = espn_get(
                f"{league}/scoreboard",
                params=params,
                cache_key=(
                    f"europe_{league}_{giorno_data}"
                )
            )

            if not data:
                continue

            for event in estrai_eventi(data):

                if not evento_terminato(event):
                    continue

                home, away = trova_competitor(event)

                if not home or not away:
                    continue

                hn = nome_squadra(home)
                an = nome_squadra(away)

                if normalizza_nome(hn) == normalizza_nome(team_name) \
                   or normalizza_nome(an) == normalizza_nome(team_name):

                    trovato.append({
                        "data": timestamp_evento(event),
                        "competizione": nome_competizione
                    })

    trovato.sort(
        key=lambda x: x["data"] or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True
    )

    return trovato[:3]


# ============================================================
# INFORTUNI
# ============================================================

def recupera_infortuni(team_id_value):
    """
    ESPN può non fornire gli infortuni per tutte le competizioni.
    In caso di assenza restituisce lista vuota.
    """

    if not team_id_value:
        return []

    # Endpoint ESPN injuries.
    # Se non disponibile, non blocchiamo il pronostico.

    try:
        url = (
            f"{ESPN_BASE}/teams/"
            f"{team_id_value}/injuries"
        )

        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT
        )

        if response.status_code != 200:
            return []

        data = response.json()

        injuries = data.get("injuries", [])

        return injuries if isinstance(
            injuries,
            list
        ) else []

    except Exception:
        return []


# ============================================================
# MOMENTUM
# ============================================================

def calcola_momentum(stats):
    if not stats:
        return 0

    ppg = stats.get("ppg")

    if ppg is None:
        return 0

    if ppg >= 2.4:
        return 25

    if ppg >= 2.0:
        return 18

    if ppg >= 1.6:
        return 10

    if ppg >= 1.2:
        return 0

    if ppg >= 0.8:
        return -10

    return -18


# ============================================================
# PROBABILITÀ
# ============================================================

def calcola_probabilita(
    home_stats,
    away_stats,
    home_home_ppg,
    away_away_ppg,
    home_fatigue,
    away_fatigue,
    h2h
):
    """
    Modello euristico trasparente.

    Non presenta le percentuali come probabilità matematiche
    certificate: sono stime basate sui dati disponibili.
    """

    score_home = 50.0
    score_draw = 25.0
    score_away = 25.0

    if home_stats.get("ppg") is not None:
        score_home += (
            home_stats["ppg"] - 1.5
        ) * 10

    if away_stats.get("ppg") is not None:
        score_away += (
            away_stats["ppg"] - 1.5
        ) * 10

    if home_home_ppg is not None:
        score_home += (
            home_home_ppg - 1.5
        ) * 8

    if away_away_ppg is not None:
        score_away += (
            away_away_ppg - 1.5
        ) * 8

    score_home += 5

    score_home -= home_fatigue * 0.05
    score_away -= away_fatigue * 0.05

    # H2H
    if h2h:

        vittorie_home = 0
        vittorie_away = 0

        for match in h2h:

            if normalizza_nome(
                match["home"]
            ) == normalizza_nome(
                match["home"]
            ):
                pass

            hg = match["hg"]
            ag = match["ag"]

            hn = normalizza_nome(
                match["home"]
            )

            if hg > ag:
                if hn == normalizza_nome(
                    match["home"]
                ):
                    vittorie_home += 1

            elif ag > hg:
                vittorie_away += 1

        score_home += vittorie_home * 1.5
        score_away += vittorie_away * 1.5

    # Pareggio legato alla distanza tra le squadre
    differenza = abs(
        score_home - score_away
    )

    score_draw += clamp(
        15 - differenza,
        3,
        15
    )

    # Normalizzazione
    score_home = max(1, score_home)
    score_draw = max(1, score_draw)
    score_away = max(1, score_away)

    totale = (
        score_home +
        score_draw +
        score_away
    )

    p1 = score_home / totale * 100
    px = score_draw / totale * 100
    p2 = score_away / totale * 100

    return (
        round(p1),
        round(px),
        round(p2)
    )


# ============================================================
# GOAL MODEL
# ============================================================

def stima_goal(
    home_stats,
    away_stats,
    home_home_ppg,
    away_away_ppg
):
    home_gf = home_stats.get("gf_media")
    home_gs = home_stats.get("gs_media")

    away_gf = away_stats.get("gf_media")
    away_gs = away_stats.get("gs_media")

    valori = [
        home_gf,
        home_gs,
        away_gf,
        away_gs
    ]

    if any(v is None for v in valori):
        return 2.45

    attacco_home = home_gf
    difesa_home = home_gs

    attacco_away = away_gf
    difesa_away = away_gs

    if home_home_ppg is not None:
        attacco_home *= (
            0.85 +
            clamp(home_home_ppg / 3, 0, 1) * 0.30
        )

    if away_away_ppg is not None:
        attacco_away *= (
            0.85 +
            clamp(away_away_ppg / 3, 0, 1) * 0.30
        )

    expected_home = (
        attacco_home +
        difesa_away
    ) / 2

    expected_away = (
        attacco_away +
        difesa_home
    ) / 2

    expected_home = clamp(
        expected_home,
        0.2,
        3.5
    )

    expected_away = clamp(
        expected_away,
        0.2,
        3.5
    )

    return clamp(
        expected_home + expected_away,
        0.5,
        6.0
    )


def probabilita_over_25(expected_goals):
    """
    Modello Poisson semplificato.
    """

    lam = expected_goals

    probabilita_0 = math.exp(-lam)

    probabilita_1 = (
        probabilita_0 * lam
    )

    probabilita_2 = (
        probabilita_1 * lam / 2
    )

    under25 = (
        probabilita_0 +
        probabilita_1 +
        probabilita_2
    )

    over25 = (
        1 - under25
    )

    return clamp(
        round(over25 * 100),
        1,
        99
    )


def probabilita_gol(
    home_stats,
    away_stats
):
    home_gf = home_stats.get("gf_media")
    away_gf = away_stats.get("gf_media")

    home_gs = home_stats.get("gs_media")
    away_gs = away_stats.get("gs_media")

    if None in (
        home_gf,
        away_gf,
        home_gs,
        away_gs
    ):
        return 55

    attacco = (
        home_gf +
        away_gf
    ) / 2

    difesa = (
        home_gs +
        away_gs
    ) / 2

    base = 48 + (
        attacco + difesa - 2
    ) * 12

    return clamp(
        round(base),
        20,
        85
    )


# ============================================================
# PRONOSTICO MIGLIORE
# ============================================================

def scegli_pronostico(
    p1,
    px,
    p2,
    over25,
    gol
):
    opzioni = [
        ("1", p1),
        ("X", px),
        ("2", p2),
        ("Over 2.5", over25),
        ("Gol", gol),
        ("Under 2.5", 100 - over25),
        ("No Gol", 100 - gol)
    ]

    opzioni.sort(
        key=lambda x: x[1],
        reverse=True
    )

    return opzioni[0]


# ============================================================
# AFFIDABILITÀ
# ============================================================

def calcola_affidabilita(
    home_form,
    away_form,
    home_standing,
    away_standing,
    h2h,
    injuries_home,
    injuries_away
):
    score = 45

    dati = 0

    if home_form:
        dati += 1

    if away_form:
        dati += 1

    if home_standing:
        dati += 1

    if away_standing:
        dati += 1

    if h2h:
        dati += 1

    if dati >= 5:
        score += 15

    elif dati >= 4:
        score += 11

    elif dati >= 2:
        score += 6

    if len(home_form) >= 5:
        score += 4

    if len(away_form) >= 5:
        score += 4

    # L'assenza di dati sugli infortuni non viene
    # interpretata come "nessun infortunio".
    if injuries_home or injuries_away:
        score += 2

    return int(
        clamp(score, 45, 88)
    )


# ============================================================
# ANALISI SINGOLA PARTITA
# ============================================================

def analizza_partita(event, league):
    home, away = trova_competitor(event)

    if not home or not away:
        return None

    home_name = nome_squadra(home)
    away_name = nome_squadra(away)

    home_id = team_id(home)
    away_id = team_id(away)

    print(
        f"   📊 Raccolta dati: "
        f"{home_name} vs {away_name}"
    )

    # --------------------------------------------------------
    # FORMA
    # --------------------------------------------------------

    home_form = recupera_form_da_eventi(
        home_name,
        league
    )

    away_form = recupera_form_da_eventi(
        away_name,
        league
    )

    home_stats = statistiche_form(
        home_form
    )

    away_stats = statistiche_form(
        away_form
    )

    print(
        f"   📈 Forma: "
        f"{home_stats['sequenza']} - "
        f"{away_stats['sequenza']}"
    )

    # --------------------------------------------------------
    # CASA / TRASFERTA
    # --------------------------------------------------------

    home_home_ppg = rendimento_casa(
        home_form
    )

    away_away_ppg = rendimento_trasferta(
        away_form
    )

    # --------------------------------------------------------
    # CLASSIFICA
    # --------------------------------------------------------

    standings = recupera_classifica(
        league
    )

    home_standing = estrai_classifica_team(
        standings,
        home_name
    )

    away_standing = estrai_classifica_team(
        standings,
        away_name
    )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h = recupera_h2h(
        home_name,
        away_name,
        league
    )

    # --------------------------------------------------------
    # FATICA
    # --------------------------------------------------------

    home_fatigue = indice_fatica(
        home_form
    )

    away_fatigue = indice_fatica(
        away_form
    )

    home_rest = giorni_dall_ultima_partita(
        home_form
    )

    away_rest = giorni_dall_ultima_partita(
        away_form
    )

    # --------------------------------------------------------
    # EUROPA
    # --------------------------------------------------------

    europe_home = verifica_impegni_europei(
        home_name
    )

    europe_away = verifica_impegni_europei(
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
    # MOMENTUM
    # --------------------------------------------------------

    momentum_home = calcola_momentum(
        home_stats
    )

    momentum_away = calcola_momentum(
        away_stats
    )

    # --------------------------------------------------------
    # PROBABILITÀ
    # --------------------------------------------------------

    p1, px, p2 = calcola_probabilita(
        home_stats,
        away_stats,
        home_home_ppg,
        away_away_ppg,
        home_fatigue,
        away_fatigue,
        h2h
    )

    # --------------------------------------------------------
    # GOAL
    # --------------------------------------------------------

    expected_goals = stima_goal(
        home_stats,
        away_stats,
        home_home_ppg,
        away_away_ppg
    )

    over25 = probabilita_over_25(
        expected_goals
    )

    gol = probabilita_gol(
        home_stats,
        away_stats
    )

    pronostico, percentuale = scegli_pronostico(
        p1,
        px,
        p2,
        over25,
        gol
    )

    affidabilita = calcola_affidabilita(
        home_form,
        away_form,
        home_standing,
        away_standing,
        h2h,
        injuries_home,
        injuries_away
    )

    # Se il dato più forte è molto basso,
    # riduciamo leggermente l'affidabilità.
    affidabilita += int(
        max(0, percentuale - 55) * 0.15
    )

    affidabilita = int(
        clamp(affidabilita, 45, 90)
    )

    return {
        "event_id": str(event.get("id", "")),
        "data": data_formattata(event),

        "home": home_name,
        "away": away_name,

        "home_form": home_stats,
        "away_form": away_stats,

        "home_home_ppg": home_home_ppg,
        "away_away_ppg": away_away_ppg,

        "home_standing": home_standing,
        "away_standing": away_standing,

        "h2h": h2h,

        "home_rest": home_rest,
        "away_rest": away_rest,

        "home_fatigue": home_fatigue,
        "away_fatigue": away_fatigue,

        "europe_home": europe_home,
        "europe_away": europe_away,

        "injuries_home": injuries_home,
        "injuries_away": injuries_away,

        "momentum_home": momentum_home,
        "momentum_away": momentum_away,

        "p1": p1,
        "px": px,
        "p2": p2,

        "expected_goals": expected_goals,
        "over25": over25,
        "under25": 100 - over25,

        "gol": gol,
        "no_gol": 100 - gol,

        "pronostico": pronostico,
        "percentuale": percentuale,

        "affidabilita": affidabilita
    }


# ============================================================
# FORMATTAZIONE
# ============================================================

def formatta_form(stats):
    if not stats:
        return "N/D"

    return (
        f"{stats.get('sequenza', 'N/D')} "
        f"(PPG {stats.get('ppg', 0):.2f}, "
        f"GF {stats.get('gf_media', 0):.2f}, "
        f"GS {stats.get('gs_media', 0):.2f})"
    )


def formatta_ppg(value):
    if value is None:
        return "N/D"

    return f"{value:.2f}"


def formatta_standing(entry):
    if not entry:
        return "N/D"

    posizione = valore_standing(
        entry,
        "rank"
    )

    punti = valore_standing(
        entry,
        "points"
    )

    if posizione is None:
        posizione = valore_standing(
            entry,
            "position"
        )

    if posizione is None and punti is None:
        return "Dati non disponibili"

    parti = []

    if posizione is not None:
        parti.append(
            f"{int(posizione)}° posto"
        )

    if punti is not None:
        parti.append(
            f"{int(punti)} pt"
        )

    return " – ".join(parti)


def descrivi_fatica(rest):
    if rest is None:
        return "N/D"

    if rest < 2:
        return "🔴 Molto alta"

    if rest < 3:
        return "🟠 Alta"

    if rest < 4:
        return "🟡 Normale"

    return "🟢 Buona"


def percentuale_bar(value):
    blocchi = int(
        clamp(value, 0, 100) / 10
    )

    return (
        "█" * blocchi +
        "░" * (10 - blocchi)
    )


# ============================================================
# REPORT
# ============================================================

def crea_report(campionato):
    config = CAMPIONATI.get(
        campionato
    )

    if not config:
        return (
            "❌ Campionato non riconosciuto."
        )

    league = config["espn"]
    nome = config["nome"]

    print("=" * 42)
    print(f"🚨 CREAREPORT: {nome}")
    print("=" * 42)

    partite = recupera_partite_future(
        campionato
    )

    if not partite:
        print(
            "⚠️ Nessuna partita futura trovata."
        )

        return (
            f"⚠️ Non ho trovato partite future "
            f"di {nome}."
        )

    partite = partite[
        :NUM_PARTITE_REPORT
    ]

    print(
        f"🎯 Partite da analizzare: "
        f"{len(partite)}"
    )

    risultati = []

    for indice, event in enumerate(
        partite,
        start=1
    ):

        home, away = trova_competitor(
            event
        )

        home_name = nome_squadra(
            home
        )

        away_name = nome_squadra(
            away
        )

        print(
            f"🔎 Analisi {indice}/{len(partite)}: "
            f"{home_name} - {away_name}"
        )

        try:

            risultato = analizza_partita(
                event,
                league
            )

            if risultato:

                risultati.append(
                    risultato
                )

                print(
                    f"✅ Pronostico creato: "
                    f"{home_name} - {away_name} "
                    f"| Affidabilità "
                    f"{risultato['affidabilita']}%"
                )

            else:

                print(
                    "⚠️ Analisi non disponibile."
                )

        except Exception as exc:

            print(
                f"❌ Errore analisi "
                f"{home_name} - {away_name}: "
                f"{exc}"
            )

            traceback.print_exc()

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

        return (
            f"⚠️ Non è stato possibile "
            f"creare pronostici per {nome}."
        )

    righe = []

    righe.append(
        f"⚽ <b>PRONOSTICI {nome.upper()}</b>"
    )

    righe.append(
        ""
    )

    righe.append(
        "📊 <i>Analisi basata sui dati "
        "disponibili al momento.</i>"
    )

    righe.append(
        "⚠️ <i>Le percentuali sono stime "
        "del modello e non garanzie.</i>"
    )

    righe.append(
        ""
    )

    # ========================================================
    # PARTITE
    # ========================================================

    for indice, r in enumerate(
        risultati,
        start=1
    ):

        righe.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        righe.append(
            f"⚽ <b>{indice}. "
            f"{r['home']} - {r['away']}</b>"
        )

        righe.append(
            f"📅 {r['data']}"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # FORMA
        # ----------------------------------------------------

        righe.append(
            "📈 <b>FORMA RECENTE</b>"
        )

        righe.append(
            f"🏠 {r['home']}: "
            f"{formatta_form(r['home_form'])}"
        )

        righe.append(
            f"✈️ {r['away']}: "
            f"{formatta_form(r['away_form'])}"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # CASA / TRASFERTA
        # ----------------------------------------------------

        righe.append(
            "🏟️ <b>CASA / TRASFERTA</b>"
        )

        righe.append(
            f"🏠 {r['home']}: "
            f"{formatta_ppg(r['home_home_ppg'])} "
            f"punti/gara in casa"
        )

        righe.append(
            f"✈️ {r['away']}: "
            f"{formatta_ppg(r['away_away_ppg'])} "
            f"punti/gara in trasferta"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # CLASSIFICA
        # ----------------------------------------------------

        righe.append(
            "🏆 <b>CLASSIFICA</b>"
        )

        righe.append(
            f"🏠 {r['home']}: "
            f"{formatta_standing(r['home_standing'])}"
        )

        righe.append(
            f"✈️ {r['away']}: "
            f"{formatta_standing(r['away_standing'])}"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # FATICA
        # ----------------------------------------------------

        righe.append(
            "💪 <b>RIPOSO / FATICA</b>"
        )

        if r["home_rest"] is None:
            righe.append(
                f"🏠 {r['home']}: N/D"
            )
        else:
            righe.append(
                f"🏠 {r['home']}: "
                f"{r['home_rest']:.1f} giorni "
                f"– {descrivi_fatica(r['home_rest'])}"
            )

        if r["away_rest"] is None:
            righe.append(
                f"✈️ {r['away']}: N/D"
            )
        else:
            righe.append(
                f"✈️ {r['away']}: "
                f"{r['away_rest']:.1f} giorni "
                f"– {descrivi_fatica(r['away_rest'])}"
            )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # EUROPA
        # ----------------------------------------------------

        righe.append(
            "🌍 <b>IMPEGNI EUROPEI RECENTI</b>"
        )

        if r["europe_home"]:

            competizioni = ", ".join(
                sorted(
                    set(
                        x["competizione"]
                        for x in r["europe_home"]
                    )
                )
            )

            righe.append(
                f"🏠 {r['home']}: {competizioni}"
            )

        else:

            righe.append(
                f"🏠 {r['home']}: nessun dato europeo recente"
            )

        if r["europe_away"]:

            competizioni = ", ".join(
                sorted(
                    set(
                        x["competizione"]
                        for x in r["europe_away"]
                    )
                )
            )

            righe.append(
                f"✈️ {r['away']}: {competizioni}"
            )

        else:

            righe.append(
                f"✈️ {r['away']}: nessun dato europeo recente"
            )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # INFORTUNI
        # ----------------------------------------------------

        righe.append(
            "🤕 <b>INFORTUNI</b>"
        )

        righe.append(
            f"🏠 {r['home']}: "
            f"{len(r['injuries_home'])} "
            f"segnalazioni disponibili"
        )

        righe.append(
            f"✈️ {r['away']}: "
            f"{len(r['injuries_away'])} "
            f"segnalazioni disponibili"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        righe.append(
            "📈 <b>MOMENTUM</b>"
        )

        righe.append(
            f"🏠 {r['home']}: "
            f"{r['momentum_home']:+d}"
        )

        righe.append(
            f"✈️ {r['away']}: "
            f"{r['momentum_away']:+d}"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # H2H
        # ----------------------------------------------------

        righe.append(
            "🤝 <b>H2H</b>"
        )

        if r["h2h"]:

            for h in r["h2h"][:3]:

                righe.append(
                    f"• {h['home']} "
                    f"{h['hg']}-{h['ag']} "
                    f"{h['away']}"
                )

        else:

            righe.append(
                "• Nessun precedente disponibile"
            )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # PROBABILITÀ
        # ----------------------------------------------------

        righe.append(
            "🎯 <b>PROBABILITÀ MODELLO</b>"
        )

        righe.append(
            f"1️⃣ 1: <b>{r['p1']}%</b> "
            f"{percentuale_bar(r['p1'])}"
        )

        righe.append(
            f"❌ X: <b>{r['px']}%</b> "
            f"{percentuale_bar(r['px'])}"
        )

        righe.append(
            f"2️⃣ 2: <b>{r['p2']}%</b> "
            f"{percentuale_bar(r['p2'])}"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # GOAL
        # ----------------------------------------------------

        righe.append(
            "⚽ <b>MERCATI GOAL</b>"
        )

        righe.append(
            f"🔥 Over 2.5: "
            f"<b>{r['over25']}%</b>"
        )

        righe.append(
            f"🧊 Under 2.5: "
            f"<b>{r['under25']}%</b>"
        )

        righe.append(
            f"🥅 Gol: "
            f"<b>{r['gol']}%</b>"
        )

        righe.append(
            f"🚫 No Gol: "
            f"<b>{r['no_gol']}%</b>"
        )

        righe.append(
            f"📊 Gol attesi stimati: "
            f"<b>{r['expected_goals']:.2f}</b>"
        )

        righe.append(
            ""
        )

        # ----------------------------------------------------
        # PRONOSTICO
        # ----------------------------------------------------

        righe.append(
            "🏆 <b>PRONOSTICO MIGLIORE</b>"
        )

        righe.append(
            f"👉 <b>{r['pronostico']}</b>"
        )

        righe.append(
            f"🎯 Probabilità stimata: "
            f"<b>{r['percentuale']}%</b>"
        )

        righe.append(
            f"⭐ Affidabilità analisi: "
            f"<b>{r['affidabilita']}%</b>"
        )

        righe.append(
            ""
        )

    righe.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    righe.append(
        "⚠️ <i>Pronostici statistici, "
        "non garanzie di risultato.</i>"
    )

    report = "\n".join(
        righe
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

    print(
        "=" * 42
    )

    return report


# ============================================================
# DIVISIONE MESSAGGI TELEGRAM
# ============================================================

def dividi_testo(
    testo,
    max_len=3900
):
    """
    Divide il testo senza spezzare inutilmente
    le righe.
    """

    if len(testo) <= max_len:
        return [testo]

    messaggi = []

    while len(testo) > max_len:

        punto = testo.rfind(
            "\n",
            0,
            max_len
        )

        if punto <= 0:
            punto = max_len

        parte = testo[:punto]

        messaggi.append(
            parte
        )

        testo = testo[punto:].lstrip()

    if testo:
        messaggi.append(
            testo
        )

    return messaggi


# ============================================================
# INVIO REPORT
# ============================================================

def invia_report_diviso(
    chat_id,
    report
):
    print("=" * 42)
    print("📨 INVIO REPORT TELEGRAM")
    print(
        f"📏 Lunghezza totale: "
        f"{len(report)} caratteri"
    )

    messaggi = dividi_testo(
        report
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
            f"{indice}/{len(messaggi)} "
            f"({len(messaggio)} caratteri)"
        )

        try:

            bot.send_message(
                chat_id,
                messaggio,
                parse_mode="HTML"
            )

            print(
                f"✅ Messaggio "
                f"{indice}/{len(messaggi)} inviato."
            )

        except Exception as exc:

            print(
                f"❌ Errore invio messaggio "
                f"{indice}/{len(messaggi)}: "
                f"{exc}"
            )

            traceback.print_exc()

            return False

        # Piccola pausa per evitare invii troppo ravvicinati
        time.sleep(0.5)

    print(
        "🏁 REPORT TELEGRAM INVIATO COMPLETAMENTE."
    )

    print("=" * 42)

    return True


# ============================================================
# ELABORAZIONE CAMPIONATO
# ============================================================

def elabora_campionato(
    chat_id,
    campionato
):
    try:

        print("=" * 42)

        print(
            f"📨 RICHIESTA CAMPIONATO: "
            f"{campionato}"
        )

        print(
            f"👤 Chat ID: {chat_id}"
        )

        print("=" * 42)

        # Messaggio iniziale
        bot.send_message(
            chat_id,
            (
                f"⚽ <b>Analizzo {campionato}</b>\n\n"
                "⏳ Sto raccogliendo forma, "
                "statistiche, classifica e altri dati...\n\n"
                "Potrebbero volerci alcuni secondi."
            )
        )

        report = crea_report(
            campionato
        )

        if not report:

            bot.send_message(
                chat_id,
                "❌ Non è stato possibile creare il report."
            )

            return

        print(
            "📊 Report generato."
        )

        print(
            "📤 Avvio invio Telegram..."
        )

        successo = invia_report_diviso(
            chat_id,
            report
        )

        if successo:

            print(
                f"✅ ELABORAZIONE "
                f"{campionato} TERMINATA."
            )

        else:

            print(
                f"❌ ELABORAZIONE "
                f"{campionato} TERMINATA CON ERRORI."
            )

        print(
            "=" * 42
        )

    except Exception as exc:

        print(
            f"❌ ERRORE GENERALE "
            f"ELABORAZIONE {campionato}: "
            f"{exc}"
        )

        traceback.print_exc()

        try:

            bot.send_message(
                chat_id,
                (
                    "❌ Si è verificato un errore "
                    "durante l'analisi.\n\n"
                    "Controlla i log di Render."
                )
            )

        except Exception:
            pass


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def comando_start(message):

    try:

        print(
            f"📩 /start ricevuto "
            f"da chat {message.chat.id}"
        )

        testo = (
            "⚽ <b>BOT PRONOSTICI CALCIO</b>\n\n"
            "Benvenuto!\n\n"
            "Scegli il campionato da analizzare:"
        )

        markup = telebot.types.ReplyKeyboardMarkup(
            resize_keyboard=True
        )

        for campionato in CAMPIONATI:
            markup.add(
                telebot.types.KeyboardButton(
                    campionato
                )
            )

        bot.send_message(
            message.chat.id,
            testo,
            reply_markup=markup
        )

    except Exception as exc:

        print(
            f"❌ Errore /start: {exc}"
        )

        traceback.print_exc()


# ============================================================
# MENU CAMPIONATI
# ============================================================

@bot.message_handler(
    func=lambda message:
    message.text in CAMPIONATI
)
def comando_campionato(message):

    campionato = message.text
    chat_id = message.chat.id

    # Lavoriamo in background per non bloccare
    # la risposta del webhook.
    thread = threading.Thread(
        target=elabora_campionato,
        args=(
            chat_id,
            campionato
        ),
        daemon=True
    )

    thread.start()


# ============================================================
# MESSAGGI NON RICONOSCIUTI
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def messaggio_generico(message):

    try:

        print(
            f"📩 Messaggio ricevuto: "
            f"{message.text}"
        )

        bot.send_message(
            message.chat.id,
            (
                "⚽ <b>Seleziona un campionato</b>\n\n"
                "Usa il menu qui sotto."
            )
        )

    except Exception as exc:

        print(
            f"❌ Errore messaggio generico: "
            f"{exc}"
        )


# ============================================================
# SERVER HTTP RENDER
# ============================================================

class HealthHandler(
    http.server.BaseHTTPRequestHandler
):

    def log_message(
        self,
        format_string,
        *args
    ):
        # Evita log HTTP inutilmente rumorosi.
        return

    def do_GET(self):

        if self.path == "/" or \
           self.path == "/health":

            risposta = {
                "status": "ok",
                "service": "bot-pronostici-gratis",
                "telegram": "webhook",
                "polling": False
            }

            body = json.dumps(
                risposta,
                ensure_ascii=False
            ).encode("utf-8")

            self.send_response(200)

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

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        print(
            f"📩 POST RICEVUTA: "
            f"{self.path}"
        )

        if self.path != WEBHOOK_PATH:

            self.send_response(404)
            self.end_headers()

            return

        try:

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

            print(
                "⚙️ Elaborazione update Telegram..."
            )

            update = telebot.types.Update.de_json(
                body.decode("utf-8")
            )

            # Risposta immediata a Telegram
            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain"
            )

            self.end_headers()

            self.wfile.write(
                b"OK"
            )

            print(
                "✅ Update Telegram elaborato."
            )

            # Elaborazione in background
            def process_update():

                try:

                    bot.process_new_updates(
                        [update]
                    )

                except Exception as exc:

                    print(
                        f"❌ Errore "
                        f"process_new_updates: "
                        f"{exc}"
                    )

                    traceback.print_exc()

            thread = threading.Thread(
                target=process_update,
                daemon=True
            )

            thread.start()

            print(
                "✅ Update inviato al thread."
            )

        except Exception as exc:

            print(
                f"❌ Errore webhook: "
                f"{exc}"
            )

            traceback.print_exc()

            try:

                self.send_response(500)
                self.end_headers()

            except Exception:
                pass


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():

    print("=" * 42)
    print("🌐 CONFIGURAZIONE WEBHOOK TELEGRAM")
    print("=" * 42)

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
            "🏁 Configurazione Telegram completata."
        )

    except Exception as exc:

        print(
            f"❌ ERRORE CONFIGURAZIONE WEBHOOK: "
            f"{exc}"
        )

        traceback.print_exc()

        raise

    print("=" * 42)


# ============================================================
# SERVER
# ============================================================

def avvia_server():

    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print("=" * 42)
    print("🚀 Avvio server HTTP Render...")
    print("=" * 42)

    print(
        "Server Render attivo."
    )

    print("=" * 42)

    print(
        f"🌐 Server HTTP avviato "
        f"sulla porta {PORT}"
    )

    print(
        f"🌐 Health URL: "
        f"{RENDER_EXTERNAL_URL}/health"
    )

    print("=" * 42)

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    thread.start()

    return server


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 42)
    print("🚀 AVVIO BOT")
    print("=" * 42)

    server = avvia_server()

    time.sleep(2)

    configura_webhook()

    print("=" * 42)
    print("✅ BOT TELEGRAM OPERATIVO")
    print("✅ MODALITÀ: WEBHOOK")
    print("❌ POLLING: DISATTIVATO")
    print("=" * 42)

    # Il processo deve rimanere vivo su Render.
    while True:

        time.sleep(60)

        print(
            "💚 Bot attivo - webhook operativo."
        )


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":
    main()
