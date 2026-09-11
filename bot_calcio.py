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
# CARICAMENTO SECRET FILE RENDER
# ============================================================

def carica_secret_file():
    """
    Carica eventuali variabili dal Secret File di Render.
    Non sovrascrive variabili già presenti nell'ambiente.
    """
    percorso = "/etc/secrets/bot_secrets.env"

    if not os.path.exists(percorso):
        return

    try:
        with open(percorso, "r", encoding="utf-8") as file:
            for riga in file:
                riga = riga.strip()

                if not riga:
                    continue

                if riga.startswith("#"):
                    continue

                if "=" not in riga:
                    continue

                nome, valore = riga.split("=", 1)

                nome = nome.strip()
                valore = valore.strip().strip('"').strip("'")

                if nome and valore and nome not in os.environ:
                    os.environ[nome] = valore

        print("✅ Secret File caricato correttamente.")

    except Exception as e:
        print(f"⚠️ Errore caricamento Secret File: {e}")


carica_secret_file()


# ============================================================
# CONFIGURAZIONE
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://bot-pronostici-gratis.onrender.com"
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = RENDER_EXTERNAL_URL + WEBHOOK_PATH


# ============================================================
# CONTROLLO CHIAVI
# ============================================================

print()
print("==========================================")
print("⚽ BOT PRONOSTICI CALCIO")
print("Avvio applicazione Render...")
print("==========================================")

print()

if TELEGRAM_BOT_TOKEN:
    print("TELEGRAM_BOT_TOKEN: OK")
else:
    print("TELEGRAM_BOT_TOKEN: MANCANTE")

if FOOTBALL_API_KEY:
    print("FOOTBALL_API_KEY: OK")
else:
    print("FOOTBALL_API_KEY: MANCANTE")

print()


# ============================================================
# TELEGRAM
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN mancante.")

if not FOOTBALL_API_KEY:
    raise RuntimeError("FOOTBALL_API_KEY mancante.")


bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# API FOOTBALL
# ============================================================

API_BASE = "https://v3.football.api-sports.io"

API_HEADERS = {
    "x-apisports-key": FOOTBALL_API_KEY
}


# ============================================================
# CAMPIONATI
# ============================================================

CAMPIONATI = {
    "🇮🇹 Serie A": 135,
    "🇬🇧 Premier League": 39,
    "🇪🇸 La Liga": 140,
    "🇩🇪 Bundesliga": 78,
    "🇫🇷 Ligue 1": 61,
}


# ID API-Football delle principali competizioni europee
CHAMPIONS_LEAGUE_ID = 2
EUROPA_LEAGUE_ID = 3
CONFERENCE_LEAGUE_ID = 848


# ============================================================
# CONFIGURAZIONE MODELLO
# ============================================================

GIORNI_PARTITE_FUTURE = 14

NUMERO_PARTITE_REPORT = 8

NUMERO_PARTITE_FORMA = 10

NUMERO_PARTITE_CALENDARIO = 8

CACHE_MINUTI = 30

TIMEOUT_API = 15


# ============================================================
# CACHE
# ============================================================

CACHE = {}

CACHE_LOCK = threading.Lock()


def cache_get(chiave):
    with CACHE_LOCK:
        elemento = CACHE.get(chiave)

        if not elemento:
            return None

        timestamp, valore = elemento

        if time.time() - timestamp > CACHE_MINUTI * 60:
            del CACHE[chiave]
            return None

        return valore


def cache_set(chiave, valore):
    with CACHE_LOCK:
        CACHE[chiave] = (time.time(), valore)


# ============================================================
# FUNZIONI GENERALI
# ============================================================

def safe_float(valore, default=0.0):
    try:
        if valore is None:
            return default

        return float(valore)

    except Exception:
        return default


def safe_int(valore, default=0):
    try:
        if valore is None:
            return default

        return int(valore)

    except Exception:
        return default


def clamp(valore, minimo, massimo):
    return max(minimo, min(massimo, valore))


def media(lista):
    valori = [safe_float(x) for x in lista]

    if not valori:
        return 0.0

    return sum(valori) / len(valori)


def normalizza_nome(nome):
    if not nome:
        return ""

    return nome.lower().strip()


def parse_data(data_string):
    if not data_string:
        return None

    try:
        return datetime.fromisoformat(
            data_string.replace("Z", "+00:00")
        )

    except Exception:
        return None


def giorni_differenza(data1, data2):
    if not data1 or not data2:
        return None

    try:
        return abs((data1 - data2).total_seconds()) / 86400
    except Exception:
        return None


# ============================================================
# RICHIESTA API FOOTBALL
# ============================================================

def api_get(endpoint, params=None, cache_key=None):
    """
    Richiesta centralizzata ad API-Football.
    Utilizza cache per ridurre il consumo giornaliero.
    """

    if cache_key:
        risultato_cache = cache_get(cache_key)

        if risultato_cache is not None:
            return risultato_cache

    url = API_BASE + endpoint

    try:
        response = requests.get(
            url,
            headers=API_HEADERS,
            params=params or {},
            timeout=TIMEOUT_API
        )

        if response.status_code != 200:
            print(
                f"⚠️ API HTTP {response.status_code}: "
                f"{endpoint}"
            )
            return None

        dati = response.json()

        if dati.get("errors"):
            print(
                f"⚠️ API errors {endpoint}: "
                f"{dati.get('errors')}"
            )

        if cache_key:
            cache_set(cache_key, dati)

        return dati

    except requests.RequestException as e:
        print(f"⚠️ Errore API {endpoint}: {e}")
        return None

    except Exception as e:
        print(f"⚠️ Errore generico API {endpoint}: {e}")
        return None


# ============================================================
# STAGIONE
# ============================================================

def stagione_corrente():
    """
    API-Football identifica la stagione con l'anno di inizio.
    A settembre 2026 => stagione 2026.
    """

    oggi = datetime.now(timezone.utc)

    if oggi.month >= 7:
        return oggi.year

    return oggi.year - 1


# ============================================================
# FIXTURE FUTURE
# ============================================================

def recupera_partite(league_id):
    """
    Recupera le prossime partite del campionato.
    """

    stagione = stagione_corrente()

    cache_key = f"future_{league_id}_{stagione}"

    dati = api_get(
        "/fixtures",
        params={
            "league": league_id,
            "season": stagione,
            "next": NUMERO_PARTITE_REPORT
        },
        cache_key=cache_key
    )

    if not dati:
        return []

    response = dati.get("response", [])

    partite = []

    for fixture in response:

        try:
            fixture_info = fixture.get("fixture", {})
            teams = fixture.get("teams", {})
            league = fixture.get("league", {})

            stato = fixture_info.get("status", {}).get("short")

            # Solo partite non ancora iniziate
            stati_validi = {
                "NS",
                "TBD",
                "PST"
            }

            if stato not in stati_validi:
                continue

            data = parse_data(fixture_info.get("date"))

            if not data:
                continue

            casa = teams.get("home", {})
            trasferta = teams.get("away", {})

            if not casa.get("id") or not trasferta.get("id"):
                continue

            partite.append({
                "id": fixture_info.get("id"),
                "date": data,
                "timestamp": fixture_info.get("timestamp"),
                "league_id": league.get("id", league_id),
                "league_name": league.get("name", ""),
                "home": {
                    "id": casa.get("id"),
                    "name": casa.get("name", "Casa"),
                    "logo": casa.get("logo")
                },
                "away": {
                    "id": trasferta.get("id"),
                    "name": trasferta.get("name", "Trasferta"),
                    "logo": trasferta.get("logo")
                }
            })

        except Exception as e:
            print(f"⚠️ Errore parsing fixture: {e}")

    partite.sort(key=lambda x: x["date"])

    return partite[:NUMERO_PARTITE_REPORT]


# ============================================================
# ULTIME PARTITE CAMPIONATO
# ============================================================

def recupera_form_squadra(team_id, league_id):
    """
    Recupera le ultime partite della squadra
    nel campionato specifico.
    """

    stagione = stagione_corrente()

    cache_key = (
        f"form_{team_id}_{league_id}_{stagione}"
    )

    dati = api_get(
        "/fixtures",
        params={
            "team": team_id,
            "league": league_id,
            "season": stagione,
            "last": NUMERO_PARTITE_FORMA
        },
        cache_key=cache_key
    )

    if not dati:
        return []

    response = dati.get("response", [])

    partite = []

    for fixture in response:

        try:
            stato = fixture.get("fixture", {}).get(
                "status", {}
            ).get("short")

            if stato not in {
                "FT",
                "AET",
                "PEN"
            }:
                continue

            data = parse_data(
                fixture.get("fixture", {}).get("date")
            )

            teams = fixture.get("teams", {})
            goals = fixture.get("goals", {})

            home = teams.get("home", {})
            away = teams.get("away", {})

            home_goals = goals.get("home")
            away_goals = goals.get("away")

            if (
                home_goals is None
                or away_goals is None
            ):
                continue

            is_home = home.get("id") == team_id

            if is_home:
                gf = safe_int(home_goals)
                ga = safe_int(away_goals)
                opponent = away.get("name", "")
            else:
                gf = safe_int(away_goals)
                ga = safe_int(home_goals)
                opponent = home.get("name", "")

            if gf > ga:
                risultato = "V"
                punti = 3

            elif gf == ga:
                risultato = "P"
                punti = 1

            else:
                risultato = "S"
                punti = 0

            partite.append({
                "date": data,
                "gf": gf,
                "ga": ga,
                "result": risultato,
                "points": punti,
                "home": is_home,
                "opponent": opponent,
                "total_goals": gf + ga
            })

        except Exception as e:
            print(
                f"⚠️ Errore form squadra {team_id}: {e}"
            )

    partite.sort(
        key=lambda x: x["date"] or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True
    )

    return partite[:NUMERO_PARTITE_FORMA]


# ============================================================
# CALENDARIO COMPLETO SQUADRA
# ============================================================

def recupera_calendario_squadra(team_id):
    """
    Recupera partite recenti e future della squadra
    per valutare riposo, congestione e impegni europei.
    """

    stagione = stagione_corrente()

    cache_key = (
        f"calendar_{team_id}_{stagione}"
    )

    oggi = datetime.now(timezone.utc)

    data_da = (
        oggi - timedelta(days=21)
    ).strftime("%Y-%m-%d")

    data_a = (
        oggi + timedelta(days=21)
    ).strftime("%Y-%m-%d")

    dati = api_get(
        "/fixtures",
        params={
            "team": team_id,
            "season": stagione,
            "from": data_da,
            "to": data_a
        },
        cache_key=cache_key
    )

    if not dati:
        return []

    response = dati.get("response", [])

    calendario = []

    for fixture in response:

        try:
            info = fixture.get("fixture", {})
            league = fixture.get("league", {})

            data = parse_data(info.get("date"))

            if not data:
                continue

            calendario.append({
                "id": info.get("id"),
                "date": data,
                "league_id": league.get("id"),
                "league_name": league.get("name", ""),
                "status": info.get("status", {}).get("short")
            })

        except Exception:
            continue

    calendario.sort(key=lambda x: x["date"])

    return calendario


# ============================================================
# CLASSIFICA
# ============================================================

def recupera_classifica(league_id):
    stagione = stagione_corrente()

    cache_key = (
        f"standings_{league_id}_{stagione}"
    )

    dati = api_get(
        "/standings",
        params={
            "league": league_id,
            "season": stagione
        },
        cache_key=cache_key
    )

    if not dati:
        return {}

    response = dati.get("response", [])

    if not response:
        return {}

    try:
        standings = response[0].get(
            "league", {}
        ).get(
            "standings", [[]]
        )[0]

    except Exception:
        return {}

    risultato = {}

    for posizione in standings:

        team = posizione.get("team", {})

        team_id = team.get("id")

        if not team_id:
            continue

        risultato[team_id] = {
            "rank": safe_int(
                posizione.get("rank")
            ),
            "points": safe_int(
                posizione.get("points")
            ),
            "played": safe_int(
                posizione.get("all", {}).get("played")
            ),
            "wins": safe_int(
                posizione.get("all", {}).get("win")
            ),
            "draws": safe_int(
                posizione.get("all", {}).get("draw")
            ),
            "losses": safe_int(
                posizione.get("all", {}).get("lose")
            ),
            "goals_for": safe_int(
                posizione.get("all", {}).get("goals", {}).get("for")
            ),
            "goals_against": safe_int(
                posizione.get("all", {}).get("goals", {}).get("against")
            )
        }

    return risultato


# ============================================================
# INFORTUNI / SQUALIFICHE
# ============================================================

def recupera_infortuni_fixture(fixture_id):
    """
    Recupera gli indisponibili della specifica partita.

    L'API può restituire infortuni, sospensioni o altri
    motivi di assenza.
    """

    cache_key = f"injuries_fixture_{fixture_id}"

    dati = api_get(
        "/injuries",
        params={
            "fixture": fixture_id
        },
        cache_key=cache_key
    )

    if not dati:
        return []

    return dati.get("response", [])


def analizza_infortuni(fixture_id, home_id, away_id):
    elementi = recupera_infortuni_fixture(fixture_id)

    risultato = {
        "home_total": 0,
        "away_total": 0,
        "home_suspensions": 0,
        "away_suspensions": 0,
        "home_weight": 0.0,
        "away_weight": 0.0
    }

    for elemento in elementi:

        try:
            team_id = elemento.get(
                "team", {}
            ).get("id")

            player = elemento.get(
                "player", {}
            )

            tipo = str(
                elemento.get("type", "")
            ).lower()

            reason = str(
                elemento.get("reason", "")
            ).lower()

            nome = player.get("name", "")

            if team_id == home_id:
                squadra = "home"

            elif team_id == away_id:
                squadra = "away"

            else:
                continue

            # Peso prudente:
            # non conosciamo necessariamente il valore
            # reale del giocatore.
            peso = 1.0

            parole_importanti = [
                "suspension",
                "suspended",
                "injury",
                "muscle",
                "knee",
                "ankle",
                "hamstring",
                "illness",
                "thigh"
            ]

            testo = f"{tipo} {reason}"

            if any(
                parola in testo
                for parola in parole_importanti
            ):
                peso = 1.0

            if squadra == "home":

                risultato["home_total"] += 1
                risultato["home_weight"] += peso

                if (
                    "susp" in tipo
                    or "susp" in reason
                ):
                    risultato["home_suspensions"] += 1

            else:

                risultato["away_total"] += 1
                risultato["away_weight"] += peso

                if (
                    "susp" in tipo
                    or "susp" in reason
                ):
                    risultato["away_suspensions"] += 1

        except Exception:
            continue

    return risultato


# ============================================================
# SCONTRI DIRETTI
# ============================================================

def recupera_head_to_head(home_id, away_id):
    """
    Recupera gli ultimi scontri diretti.
    """

    cache_key = (
        f"h2h_{min(home_id, away_id)}_"
        f"{max(home_id, away_id)}"
    )

    dati = api_get(
        "/fixtures/headtohead",
        params={
            "h2h": f"{home_id}-{away_id}",
            "last": 10
        },
        cache_key=cache_key
    )

    if not dati:
        return []

    response = dati.get("response", [])

    risultati = []

    for fixture in response:

        try:
            stato = fixture.get(
                "fixture", {}
            ).get(
                "status", {}
            ).get("short")

            if stato not in {
                "FT",
                "AET",
                "PEN"
            }:
                continue

            teams = fixture.get("teams", {})
            goals = fixture.get("goals", {})

            home = teams.get("home", {})
            away = teams.get("away", {})

            hg = goals.get("home")
            ag = goals.get("away")

            if hg is None or ag is None:
                continue

            risultati.append({
                "home_id": home.get("id"),
                "away_id": away.get("id"),
                "home_goals": safe_int(hg),
                "away_goals": safe_int(ag)
            })

        except Exception:
            continue

    return risultati[:10]


# ============================================================
# PREDIZIONE API-FOOTBALL
# ============================================================

def recupera_predizione_api(fixture_id):
    """
    Recupera la previsione indipendente di API-Football.
    Viene utilizzata solo come uno dei segnali del modello.
    """

    cache_key = f"prediction_{fixture_id}"

    dati = api_get(
        "/predictions",
        params={
            "fixture": fixture_id
        },
        cache_key=cache_key
    )

    if not dati:
        return None

    response = dati.get("response", [])

    if not response:
        return None

    return response[0]


# ============================================================
# STATISTICHE FORMA
# ============================================================

def calcola_statistiche_form(partite):
    if not partite:
        return {
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "points": 0,
            "ppg": 0,
            "gf_avg": 0,
            "ga_avg": 0,
            "total_avg": 0,
            "over15": 0,
            "over25": 0,
            "btts": 0,
            "form": "",
            "clean_sheets": 0,
            "failed_to_score": 0
        }

    wins = sum(
        1 for x in partite
        if x["result"] == "V"
    )

    draws = sum(
        1 for x in partite
        if x["result"] == "P"
    )

    losses = sum(
        1 for x in partite
        if x["result"] == "S"
    )

    points = sum(
        x["points"] for x in partite
    )

    gf_avg = media(
        [x["gf"] for x in partite]
    )

    ga_avg = media(
        [x["ga"] for x in partite]
    )

    total_avg = media(
        [x["total_goals"] for x in partite]
    )

    over15 = (
        sum(
            1 for x in partite
            if x["total_goals"] >= 2
        ) / len(partite)
    )

    over25 = (
        sum(
            1 for x in partite
            if x["total_goals"] >= 3
        ) / len(partite)
    )

    btts = (
        sum(
            1 for x in partite
            if x["gf"] >= 1 and x["ga"] >= 1
        ) / len(partite)
    )

    clean_sheets = sum(
        1 for x in partite
        if x["ga"] == 0
    )

    failed_to_score = sum(
        1 for x in partite
        if x["gf"] == 0
    )

    return {
        "played": len(partite),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points": points,
        "ppg": points / len(partite),
        "gf_avg": gf_avg,
        "ga_avg": ga_avg,
        "total_avg": total_avg,
        "over15": over15,
        "over25": over25,
        "btts": btts,
        "form": "".join(
            x["result"] for x in reversed(partite)
        ),
        "clean_sheets": clean_sheets,
        "failed_to_score": failed_to_score
    }


# ============================================================
# FORMA CASA / TRASFERTA
# ============================================================

def calcola_split(partite, casa):
    """
    Calcola le prestazioni specifiche casa/trasferta.
    """

    filtrate = [
        p for p in partite
        if p["home"] == casa
    ]

    return calcola_statistiche_form(filtrate)


# ============================================================
# HEAD TO HEAD STATISTICS
# ============================================================

def calcola_h2h(head2head, home_id, away_id):

    if not head2head:
        return {
            "home_wins": 0,
            "draws": 0,
            "away_wins": 0,
            "total": 0,
            "home_rate": 0.0,
            "away_rate": 0.0
        }

    home_wins = 0
    draws = 0
    away_wins = 0

    for partita in head2head:

        hg = partita["home_goals"]
        ag = partita["away_goals"]

        home_team_id = partita["home_id"]

        if hg == ag:
            draws += 1

        elif (
            hg > ag
            and home_team_id == home_id
        ):
            home_wins += 1

        elif (
            hg < ag
            and home_team_id == away_id
        ):
            away_wins += 1

        elif (
            hg > ag
            and home_team_id == away_id
        ):
            away_wins += 1

        else:
            home_wins += 1

    totale = len(head2head)

    return {
        "home_wins": home_wins,
        "draws": draws,
        "away_wins": away_wins,
        "total": totale,
        "home_rate": home_wins / totale,
        "away_rate": away_wins / totale
    }


# ============================================================
# CALENDARIO / FATICA
# ============================================================

def analizza_fatica(calendario, data_partita):

    if not calendario or not data_partita:
        return {
            "days_rest": None,
            "matches_7": 0,
            "matches_14": 0,
            "europe_before": False,
            "europe_after": False,
            "fatigue": 0
        }

    precedenti = []

    successivi = []

    for partita in calendario:

        data = partita["date"]

        if data < data_partita:
            precedenti.append(partita)

        elif data > data_partita:
            successivi.append(partita)

    precedenti.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    ultima = precedenti[0] if precedenti else None

    days_rest = None

    if ultima:
        days_rest = (
            data_partita - ultima["date"]
        ).total_seconds() / 86400

    inizio_7 = data_partita - timedelta(days=7)

    inizio_14 = data_partita - timedelta(days=14)

    partite_7 = [
        p for p in precedenti
        if p["date"] >= inizio_7
    ]

    partite_14 = [
        p for p in precedenti
        if p["date"] >= inizio_14
    ]

    def europea(p):

        league_id = p.get("league_id")

        nome = str(
            p.get("league_name", "")
        ).lower()

        if league_id in {
            CHAMPIONS_LEAGUE_ID,
            EUROPA_LEAGUE_ID,
            CONFERENCE_LEAGUE_ID
        }:
            return True

        parole = [
            "champions",
            "europa league",
            "conference league"
        ]

        return any(
            parola in nome
            for parola in parole
        )

    europe_before = any(
        europea(p)
        for p in precedenti
        if p["date"] >= data_partita - timedelta(days=5)
    )

    europe_after = any(
        europea(p)
        for p in successivi
        if p["date"] <= data_partita + timedelta(days=5)
    )

    fatigue = 0

    if days_rest is not None:

        if days_rest < 2.0:
            fatigue += 20

        elif days_rest < 3.0:
            fatigue += 14

        elif days_rest < 4.0:
            fatigue += 7

    if len(partite_7) >= 3:
        fatigue += 12

    elif len(partite_7) >= 2:
        fatigue += 5

    if len(partite_14) >= 5:
        fatigue += 10

    if europe_before:
        fatigue += 8

    fatigue = clamp(
        fatigue,
        0,
        40
    )

    return {
        "days_rest": days_rest,
        "matches_7": len(partite_7),
        "matches_14": len(partite_14),
        "europe_before": europe_before,
        "europe_after": europe_after,
        "fatigue": fatigue
    }


# ============================================================
# ANALISI MOMENTO / PRESSIONE
# ============================================================

def analizza_momento(statistiche, classifica):
    """
    Non tenta di leggere la psicologia reale.
    Utilizza indicatori misurabili:
    - risultati recenti
    - serie
    - posizione
    - punti
    """

    score = 0.0

    if not statistiche:
        return score

    score += (
        statistiche["ppg"] - 1.2
    ) * 10

    form = statistiche.get("form", "")

    if form.endswith("VV"):
        score += 5

    elif form.endswith("SS"):
        score -= 5

    if statistiche["failed_to_score"] >= 4:
        score -= 4

    if statistiche["clean_sheets"] >= 4:
        score += 4

    if classifica:

        rank = classifica.get("rank", 0)

        points = classifica.get("points", 0)

        if rank:

            if rank <= 4:
                score += 4

            elif rank >= 17:
                score -= 4

        if points >= 20:
            score += 2

    return clamp(
        score,
        -15,
        15
    )


# ============================================================
# ANALISI CLASSIFICA
# ============================================================

def differenza_classifica(home_table, away_table):

    if not home_table or not away_table:
        return 0.0

    home_rank = home_table.get("rank", 0)
    away_rank = away_table.get("rank", 0)

    if not home_rank or not away_rank:
        return 0.0

    differenza = away_rank - home_rank

    return clamp(
        differenza * 1.2,
        -15,
        15
    )


# ============================================================
# PROBABILITÀ DA STATISTICHE
# ============================================================

def probabilita_da_forze(
    forza_home,
    forza_away
):

    differenza = forza_home - forza_away

    # Sigmoid
    p_home = 1 / (
        1 + math.exp(
            -differenza / 8
        )
    )

    # Forza relativa
    p_home = clamp(
        p_home,
        0.20,
        0.70
    )

    # Pareggio stimato
    p_draw = (
        0.28
        - abs(differenza) * 0.006
    )

    p_draw = clamp(
        p_draw,
        0.16,
        0.30
    )

    p_away = 1 - p_home - p_draw

    if p_away < 0.10:
        p_away = 0.10

        totale = p_home + p_draw + p_away

        p_home /= totale
        p_draw /= totale
        p_away /= totale

    return {
        "home": p_home,
        "draw": p_draw,
        "away": p_away
    }


# ============================================================
# PROBABILITÀ DALL'API PREDICTIONS
# ============================================================

def estrai_probabilita_api(predizione):

    risultato = {
        "home": None,
        "draw": None,
        "away": None
    }

    if not predizione:
        return risultato

    predictions = predizione.get(
        "predictions",
        {}
    )

    percentuali = predictions.get(
        "percent"
    )

    if not percentuali:
        return risultato

    risultato["home"] = parse_percentuale(
        percentuali.get("home")
    )

    risultato["draw"] = parse_percentuale(
        percentuali.get("draw")
    )

    risultato["away"] = parse_percentuale(
        percentuali.get("away")
    )

    return risultato


def parse_percentuale(valore):

    if valore is None:
        return None

    try:
        testo = str(valore).replace("%", "").strip()

        numero = float(testo)

        if numero > 1:
            return numero / 100

        return numero

    except Exception:
        return None


# ============================================================
# MODELLO PRINCIPALE
# ============================================================

def genera_pronostico_avanzato(
    fixture,
    home_form,
    away_form,
    home_calendar,
    away_calendar,
    home_table,
    away_table,
    h2h,
    injuries,
    api_prediction
):

    home_stats = calcola_statistiche_form(
        home_form
    )

    away_stats = calcola_statistiche_form(
        away_form
    )

    home_home = calcola_split(
        home_form,
        True
    )

    away_away = calcola_split(
        away_form,
        False
    )

    # --------------------------------------------------------
    # FORZA BASE
    # --------------------------------------------------------

    forza_home = 50.0
    forza_away = 50.0

    # Forma generale
    forza_home += (
        home_stats["ppg"] - 1.3
    ) * 8

    forza_away += (
        away_stats["ppg"] - 1.3
    ) * 8

    # Casa / trasferta
    forza_home += (
        home_home["ppg"] - 1.3
    ) * 10

    forza_away += (
        away_away["ppg"] - 1.3
    ) * 10

    # Gol
    forza_home += (
        home_stats["gf_avg"]
        - home_stats["ga_avg"]
    ) * 3

    forza_away += (
        away_stats["gf_avg"]
        - away_stats["ga_avg"]
    ) * 3

    # Vantaggio campo
    forza_home += 5.0

    # Classifica
    forza_home += differenza_classifica(
        home_table,
        away_table
    )

    forza_away -= differenza_classifica(
        home_table,
        away_table
    )

    # Momento
    forza_home += analizza_momento(
        home_stats,
        home_table
    )

    forza_away += analizza_momento(
        away_stats,
        away_table
    )

    # --------------------------------------------------------
    # HEAD TO HEAD
    # --------------------------------------------------------

    h2h_stats = calcola_h2h(
        h2h,
        fixture["home"]["id"],
        fixture["away"]["id"]
    )

    if h2h_stats["total"] >= 3:

        forza_home += (
            h2h_stats["home_rate"] - 0.33
        ) * 8

        forza_away += (
            h2h_stats["away_rate"] - 0.33
        ) * 8

    # --------------------------------------------------------
    # FATICA
    # --------------------------------------------------------

    home_fatigue = analizza_fatica(
        home_calendar,
        fixture["date"]
    )

    away_fatigue = analizza_fatica(
        away_calendar,
        fixture["date"]
    )

    forza_home -= (
        home_fatigue["fatigue"] * 0.35
    )

    forza_away -= (
        away_fatigue["fatigue"] * 0.35
    )

    # --------------------------------------------------------
    # INFORTUNI / SQUALIFICHE
    # --------------------------------------------------------

    forza_home -= (
        injuries["home_weight"] * 1.2
    )

    forza_away -= (
        injuries["away_weight"] * 1.2
    )

    forza_home = clamp(
        forza_home,
        20,
        90
    )

    forza_away = clamp(
        forza_away,
        20,
        90
    )

    # --------------------------------------------------------
    # PROBABILITÀ MODELLO
    # --------------------------------------------------------

    probabilita_model = probabilita_da_forze(
        forza_home,
        forza_away
    )

    # --------------------------------------------------------
    # FUSIONE CON API-FOOTBALL
    # --------------------------------------------------------

    probabilita_api = estrai_probabilita_api(
        api_prediction
    )

    for esito in [
        "home",
        "draw",
        "away"
    ]:

        p_model = probabilita_model[esito]
        p_api = probabilita_api[esito]

        if p_api is not None:

            # 70% modello nostro
            # 30% modello API-Football
            probabilita_model[esito] = (
                p_model * 0.70
                + p_api * 0.30
            )

    # Normalizzazione
    totale = sum(
        probabilita_model.values()
    )

    if totale > 0:

        for esito in probabilita_model:
            probabilita_model[esito] /= totale

    # --------------------------------------------------------
    # RISULTATO PRINCIPALE
    # --------------------------------------------------------

    p_home = probabilita_model["home"]
    p_draw = probabilita_model["draw"]
    p_away = probabilita_model["away"]

    migliore = max(
        probabilita_model,
        key=probabilita_model.get
    )

    p_migliore = probabilita_model[migliore]

    if migliore == "home":
        esito_secco = "1"

    elif migliore == "away":
        esito_secco = "2"

    else:
        esito_secco = "X"

    # --------------------------------------------------------
    # DOPPIA CHANCE
    # --------------------------------------------------------

    p_1x = p_home + p_draw
    p_x2 = p_draw + p_away

    if p_1x >= 0.68 and p_1x >= p_x2:
        doppia_chance = "1X"
        p_doppia = p_1x

    elif p_x2 >= 0.68:
        doppia_chance = "X2"
        p_doppia = p_x2

    else:
        doppia_chance = esito_secco
        p_doppia = p_migliore

    # --------------------------------------------------------
    # GOAL
    # --------------------------------------------------------

    media_gol = (
        home_stats["gf_avg"]
        + away_stats["gf_avg"]
        + home_stats["ga_avg"]
        + away_stats["ga_avg"]
    ) / 2

    # Correzione casa/trasferta
    media_gol = (
        media_gol * 0.65
        + (
            home_home["gf_avg"]
            + away_away["gf_avg"]
        ) * 0.35
    )

    media_gol = clamp(
        media_gol,
        1.0,
        5.0
    )

    over15_base = (
        home_stats["over15"] * 0.35
        + away_stats["over15"] * 0.35
        + clamp(
            (media_gol - 1.0) / 2.0,
            0.0,
            1.0
        ) * 0.30
    )

    over25_base = (
        home_stats["over25"] * 0.35
        + away_stats["over25"] * 0.35
        + clamp(
            (media_gol - 2.0) / 1.8,
            0.0,
            1.0
        ) * 0.30
    )

    over15_base = clamp(
        over15_base,
        0.45,
        0.92
    )

    over25_base = clamp(
        over25_base,
        0.20,
        0.82
    )

    if over15_base >= 0.72:
        gol_linea = "OVER 1.5"

    else:
        gol_linea = "UNDER 1.5"

    # --------------------------------------------------------
    # BTTS
    # --------------------------------------------------------

    btts_prob = (
        home_stats["btts"] * 0.40
        + away_stats["btts"] * 0.40
        + clamp(
            media_gol / 4.0,
            0.0,
            1.0
        ) * 0.20
    )

    btts_prob = clamp(
        btts_prob,
        0.20,
        0.85
    )

    if btts_prob >= 0.55:
        btts = "GOL"

    else:
        btts = "NO GOL"

    # --------------------------------------------------------
    # AFFIDABILITÀ
    # --------------------------------------------------------

    qualita_dati = 0

    if len(home_form) >= 5:
        qualita_dati += 15

    if len(away_form) >= 5:
        qualita_dati += 15

    if home_table:
        qualita_dati += 10

    if away_table:
        qualita_dati += 10

    if h2h:
        qualita_dati += 5

    if api_prediction:
        qualita_dati += 10

    if home_calendar:
        qualita_dati += 10

    if away_calendar:
        qualita_dati += 10

    # Base affidabilità
    affidabilita = 50 + qualita_dati * 0.25

    # Forza del segnale
    affidabilita += (
        abs(p_migliore - 0.33) * 30
    )

    # Dati insufficienti => forte penalizzazione
    if (
        len(home_form) < 5
        or len(away_form) < 5
    ):
        affidabilita -= 12

    # Pareggio molto vicino alle altre possibilità
    valori = sorted(
        probabilita_model.values(),
        reverse=True
    )

    if len(valori) >= 2:

        distanza = valori[0] - valori[1]

        if distanza < 0.05:
            affidabilita -= 10

        elif distanza > 0.15:
            affidabilita += 6

    affidabilita = clamp(
        affidabilita,
        52,
        88
    )

    # --------------------------------------------------------
    # QUALITÀ FINALE
    # --------------------------------------------------------

    return {
        "esito": esito_secco,
        "doppia_chance": doppia_chance,
        "gol": gol_linea,
        "btts": btts,
        "affidabilita": round(
            affidabilita
        ),
        "probabilita": {
            "1": round(p_home * 100),
            "X": round(p_draw * 100),
            "2": round(p_away * 100)
        },
        "media_gol": round(
            media_gol,
            2
        ),
        "fatica_home": home_fatigue,
        "fatica_away": away_fatigue,
        "injuries": injuries,
        "data_quality": qualita_dati
    }


# ============================================================
# FORMATTAZIONE PRONOSTICO
# ============================================================

def formatta_pronostico(
    fixture,
    pronostico
):

    casa = fixture["home"]["name"]
    trasferta = fixture["away"]["name"]

    esito = pronostico["doppia_chance"]

    # Se il modello è molto forte, utilizziamo il segno secco.
    if (
        pronostico["probabilita"].get(
            pronostico["esito"],
            0
        ) >= 52
        and pronostico["affidabilita"] >= 70
    ):
        esito = pronostico["esito"]

    return (
        f"⚽ {casa} - {trasferta}\n\n"
        f"🔮 PRONOSTICO MIGLIORE\n"
        f"{esito}\n\n"
        f"📈 {pronostico['gol']}\n"
        f"⚽ {pronostico['btts']}\n\n"
        f"🎯 AFFIDABILITÀ MODELLO: "
        f"{pronostico['affidabilita']}%"
    )


# ============================================================
# ANALISI COMPLETA DI UNA PARTITA
# ============================================================

def analizza_partita(fixture):

    home_id = fixture["home"]["id"]
    away_id = fixture["away"]["id"]

    league_id = fixture["league_id"]

    print()
    print(
        f"🔎 ANALISI: "
        f"{fixture['home']['name']} - "
        f"{fixture['away']['name']}"
    )

    # --------------------------------------------------------
    # FORMA
    # --------------------------------------------------------

    home_form = recupera_form_squadra(
        home_id,
        league_id
    )

    away_form = recupera_form_squadra(
        away_id,
        league_id
    )

    print(
        f"📊 Forma: "
        f"{len(home_form)} / "
        f"{len(away_form)} partite"
    )

    # --------------------------------------------------------
    # CLASSIFICA
    # --------------------------------------------------------

    classifica = recupera_classifica(
        league_id
    )

    home_table = classifica.get(
        home_id,
        {}
    )

    away_table = classifica.get(
        away_id,
        {}
    )

    # --------------------------------------------------------
    # CALENDARIO
    # --------------------------------------------------------

    home_calendar = recupera_calendario_squadra(
        home_id
    )

    away_calendar = recupera_calendario_squadra(
        away_id
    )

    # --------------------------------------------------------
    # SCONTRI DIRETTI
    # --------------------------------------------------------

    h2h = recupera_head_to_head(
        home_id,
        away_id
    )

    # --------------------------------------------------------
    # INFORTUNI
    # --------------------------------------------------------

    injuries = analizza_infortuni(
        fixture["id"],
        home_id,
        away_id
    )

    # --------------------------------------------------------
    # PREDIZIONE ESTERNA
    # --------------------------------------------------------

    api_prediction = recupera_predizione_api(
        fixture["id"]
    )

    # --------------------------------------------------------
    # MODELLO
    # --------------------------------------------------------

    pronostico = genera_pronostico_avanzato(
        fixture=fixture,
        home_form=home_form,
        away_form=away_form,
        home_calendar=home_calendar,
        away_calendar=away_calendar,
        home_table=home_table,
        away_table=away_table,
        h2h=h2h,
        injuries=injuries,
        api_prediction=api_prediction
    )

    print(
        f"✅ Pronostico: "
        f"{pronostico['doppia_chance']} | "
        f"{pronostico['gol']} | "
        f"{pronostico['btts']} | "
        f"{pronostico['affidabilita']}%"
    )

    return pronostico


# ============================================================
# CREAZIONE REPORT
# ============================================================

def crea_report(nome_campionato, league_id):

    print()
    print("==========================================")
    print(
        f"🚨 CREAREPORT: "
        f"{nome_campionato} | ID: {league_id}"
    )
    print("==========================================")

    partite = recupera_partite(
        league_id
    )

    if not partite:
        return (
            f"⚠️ Non sono state trovate "
            f"partite future per {nome_campionato}."
        )

    print(
        f"📅 Partite trovate: {len(partite)}"
    )

    risultati = []

    for indice, fixture in enumerate(
        partite,
        start=1
    ):

        try:

            pronostico = analizza_partita(
                fixture
            )

            testo = formatta_pronostico(
                fixture,
                pronostico
            )

            risultati.append(
                f"{indice}. {testo}"
            )

        except Exception as e:

            print(
                f"❌ Errore analisi "
                f"{fixture.get('id')}: {e}"
            )

            continue

    if not risultati:

        return (
            "⚠️ Non è stato possibile "
            "calcolare i pronostici."
        )

    intestazione = (
        f"⚽ PRONOSTICI {nome_campionato}\n\n"
        f"🧠 Analisi avanzata automatica\n"
        f"📊 Forma + classifica + casa/trasferta\n"
        f"🌍 Calendario europeo + riposo\n"
        f"🏥 Indisponibili + squalifiche\n"
        f"📚 Scontri diretti + modello statistico\n"
        f"🤖 Modello API-Football\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    return intestazione + (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(risultati)


# ============================================================
# TELEGRAM / START
# ============================================================

@bot.message_handler(commands=["start"])
def comando_start(message):

    print(
        f"📩 /start da chat "
        f"{message.chat.id}"
    )

    testo = (
        "⚽ BENVENUTO NEL BOT PRONOSTICI CALCIO!\n\n"
        "🤖 Il modello analizza automaticamente "
        "i principali fattori disponibili.\n\n"
        "📊 Forma\n"
        "🏠 Casa / Trasferta\n"
        "⚽ Gol fatti e subiti\n"
        "📈 Classifica\n"
        "🌍 Impegni europei\n"
        "⏱️ Riposo e calendario\n"
        "🏥 Indisponibili\n"
        "📚 Scontri diretti\n"
        "🧠 Momento della squadra\n"
        "🤖 Modello statistico\n\n"
        "👇 Scegli un campionato:"
    )

    bot.send_message(
        message.chat.id,
        testo
    )

    invia_menu_campionati(
        message.chat.id
    )


# ============================================================
# HELP
# ============================================================

@bot.message_handler(commands=["help"])
def comando_help(message):

    testo = (
        "ℹ️ COME FUNZIONA IL BOT\n\n"
        "Il bot raccoglie automaticamente "
        "i dati disponibili e li combina "
        "in un unico modello statistico.\n\n"
        "Il risultato mostrato è solamente "
        "il pronostico finale, senza mostrare "
        "tutta l'analisi interna.\n\n"
        "⚠️ Nessun pronostico può garantire "
        "il risultato di una partita."
    )

    bot.send_message(
        message.chat.id,
        testo
    )


# ============================================================
# MENU CAMPIONATI
# ============================================================

def invia_menu_campionati(chat_id):

    testo = (
        "🏆 CAMPIONATI DISPONIBILI\n\n"
        "🇮🇹 Serie A\n"
        "🇬🇧 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "Scrivi il nome del campionato."
    )

    bot.send_message(
        chat_id,
        testo
    )


@bot.message_handler(commands=["campionati"])
def comando_campionati(message):

    invia_menu_campionati(
        message.chat.id
    )


# ============================================================
# RICONOSCIMENTO CAMPIONATO
# ============================================================

def trova_campionato(testo):

    testo_normalizzato = (
        testo.lower()
        .strip()
    )

    for nome, league_id in CAMPIONATI.items():

        nome_pulito = (
            nome.replace("🇮🇹", "")
            .replace("🇬🇧", "")
            .replace("🇪🇸", "")
            .replace("🇩🇪", "")
            .replace("🇫🇷", "")
            .strip()
            .lower()
        )

        if testo_normalizzato == nome_pulito:
            return nome, league_id

        if nome_pulito in testo_normalizzato:
            return nome, league_id

    # Alias
    alias = {
        "serie a": "🇮🇹 Serie A",
        "seriea": "🇮🇹 Serie A",
        "premier": "🇬🇧 Premier League",
        "premier league": "🇬🇧 Premier League",
        "liga": "🇪🇸 La Liga",
        "la liga": "🇪🇸 La Liga",
        "bundesliga": "🇩🇪 Bundesliga",
        "ligue 1": "🇫🇷 Ligue 1",
        "ligue1": "🇫🇷 Ligue 1"
    }

    if testo_normalizzato in alias:

        nome = alias[
            testo_normalizzato
        ]

        return nome, CAMPIONATI[nome]

    return None, None


# ============================================================
# HANDLER CAMPIONATO
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def gestisci_messaggio(message):

    testo = message.text or ""

    nome_campionato, league_id = trova_campionato(
        testo
    )

    if not league_id:

        bot.send_message(
            message.chat.id,
            "⚽ Scrivi uno di questi campionati:\n\n"
            "🇮🇹 Serie A\n"
            "🇬🇧 Premier League\n"
            "🇪🇸 La Liga\n"
            "🇩🇪 Bundesliga\n"
            "🇫🇷 Ligue 1"
        )

        return

    print()
    print(
        f"📩 Richiesta campionato: "
        f"{nome_campionato}"
    )

    # Messaggio immediato
    messaggio_attesa = bot.send_message(
        message.chat.id,
        "🧠 Sto analizzando le partite...\n\n"
        "📊 Forma\n"
        "🏠 Casa/Trasferta\n"
        "📈 Classifica\n"
        "🌍 Calendario europeo\n"
        "⏱️ Riposo e fatica\n"
        "🏥 Indisponibili\n"
        "📚 Scontri diretti\n"
        "🤖 Modello statistico\n\n"
        "⏳ Attendi qualche secondo..."
    )

    try:

        report = crea_report(
            nome_campionato,
            league_id
        )

        # Telegram ha un limite di lunghezza dei messaggi.
        # Dividiamo automaticamente il report.
        invia_report_diviso(
            message.chat.id,
            report
        )

        # Prova a cancellare il messaggio di attesa
        try:
            bot.delete_message(
                message.chat.id,
                messaggio_attesa.message_id
            )
        except Exception:
            pass

    except Exception as e:

        print(
            f"❌ Errore creazione report: {e}"
        )

        try:
            bot.edit_message_text(
                "❌ Si è verificato un errore "
                "durante l'analisi.\n\n"
                "Riprova tra poco.",
                message.chat.id,
                messaggio_attesa.message_id
            )

        except Exception:
            bot.send_message(
                message.chat.id,
                "❌ Errore durante l'analisi. "
                "Riprova tra poco."
            )


# ============================================================
# INVIO REPORT DIVISO
# ============================================================

def invia_report_diviso(chat_id, testo):

    limite = 3900

    if len(testo) <= limite:

        bot.send_message(
            chat_id,
            testo
        )

        return

    blocchi = []

    corrente = ""

    for parte in testo.split(
        "\n\n"
    ):

        candidato = (
            corrente
            + ("\n\n" if corrente else "")
            + parte
        )

        if len(candidato) > limite:

            if corrente:
                blocchi.append(
                    corrente
                )

            corrente = parte

        else:
            corrente = candidato

    if corrente:
        blocchi.append(
            corrente
        )

    for blocco in blocchi:

        bot.send_message(
            chat_id,
            blocco
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

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(self):

        print(
            f"🌐 GET ricevuta: "
            f"{self.path}"
        )

        if self.path == "/":

            risposta = (
                "Bot pronostici calcio "
                "online."
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

        if self.path == "/health":

            risposta = "OK"

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

    # --------------------------------------------------------
    # POST WEBHOOK TELEGRAM
    # --------------------------------------------------------

    def do_POST(self):

        print()
        print(
            f"📩 POST RICEVUTA: "
            f"{self.path}"
        )

        if self.path != WEBHOOK_PATH:

            print(
                "⚠️ POST ricevuta su "
                "percorso sconosciuto."
            )

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

            update_json = json.loads(
                body.decode("utf-8")
            )

            update = telebot.types.Update.de_json(
                update_json
            )

            # Rispondiamo subito a Telegram
            # per evitare retry inutili.
            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain"
            )

            self.end_headers()

            self.wfile.write(
                b"OK"
            )

            self.wfile.flush()

            # Elaborazione in thread separato
            # per non bloccare il webhook.
            thread = threading.Thread(
                target=elabora_update,
                args=(update,),
                daemon=True
            )

            thread.start()

            print(
                "✅ Update inviato al thread."
            )

            print(
                "✅ Update Telegram elaborato."
            )

        except Exception as e:

            print(
                f"❌ Errore webhook: {e}"
            )

            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except Exception:
                pass


# ============================================================
# ELABORAZIONE UPDATE
# ============================================================

def elabora_update(update):

    try:

        bot.process_new_updates(
            [update]
        )

    except Exception as e:

        print(
            f"❌ Errore process_new_updates: "
            f"{e}"
        )


# ============================================================
# SERVER
# ============================================================

def avvia_server():

    server = http.server.ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    print()
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
        f"🔗 Webhook: {WEBHOOK_URL}"
    )

    server.serve_forever()


# ============================================================
# WEBHOOK
# ============================================================

def configura_webhook():

    print()
    print(
        "=========================================="
    )
    print(
        "CONFIGURAZIONE TELEGRAM"
    )
    print(
        "=========================================="
    )

    try:

        # Rimuove eventuali webhook precedenti
        # senza utilizzare il polling.
        bot.remove_webhook()

        time.sleep(1)

        risultato = bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True
        )

        print(
            f"🔗 Webhook impostato: "
            f"{WEBHOOK_URL}"
        )

        print(
            f"📡 Risultato set_webhook: "
            f"{risultato}"
        )

        time.sleep(1)

        info = bot.get_webhook_info()

        print()
        print(
            "📡 TELEGRAM WEBHOOK INFO"
        )

        print(
            f"URL: {info.url}"
        )

        print(
            f"Pending update: "
            f"{info.pending_update_count}"
        )

        if info.last_error_message:
            print(
                f"⚠️ Ultimo errore: "
                f"{info.last_error_message}"
            )

        else:
            print(
                "✅ Nessun errore webhook."
            )

    except Exception as e:

        print(
            f"❌ Errore configurazione webhook: "
            f"{e}"
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print()
    print(
        "🚀 Avvio Bot Pronostici..."
    )

    # Server HTTP per Render
    server_thread = threading.Thread(
        target=avvia_server,
        daemon=True
    )

    server_thread.start()

    # Aspetta che il server sia disponibile
    time.sleep(2)

    # Configura Telegram
    configura_webhook()

    print()
    print(
        "=========================================="
    )
    print(
        "✅ BOT ONLINE"
    )
    print(
        "=========================================="
    )

    print(
        f"🤖 Bot pronto."
    )

    print(
        f"🌐 {RENDER_EXTERNAL_URL}"
    )

    print(
        f"📡 {WEBHOOK_URL}"
    )

    print(
        "=========================================="
    )

    # Mantiene vivo il processo Render
    while True:

        time.sleep(60)
