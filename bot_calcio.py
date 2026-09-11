import os
import json
import time
import threading
import http.server
import socketserver
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
import telebot


# ============================================================
# CONFIGURAZIONE
# ============================================================

SECRET_FILE = "/etc/secrets/bot_secrets.env"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

RENDER_PORT = int(os.getenv("PORT", "10000"))

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"

API_TIMEOUT = 30

NUMERO_PARTITE_REPORT = 8
NUMERO_PARTITE_FORM = 10


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
# VARIABILI GLOBALI
# ============================================================

bot = None

cache_api = {}
cache_sportsdb = {}

CACHE_API_SECONDS = 300
CACHE_SPORTSDB_SECONDS = 900


# ============================================================
# LETTURA SECRET FILE RENDER
# ============================================================

def carica_secret_file():
    """
    Carica le variabili dal Secret File di Render, se presente.
    """

    if not os.path.exists(SECRET_FILE):
        return

    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as file:
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

                if nome and valore:
                    os.environ[nome] = valore

        print("✅ Secret File caricato correttamente.")

    except Exception as errore:
        print(f"⚠️ Errore lettura Secret File: {errore}")


# ============================================================
# RICARICA TOKEN DOPO SECRET FILE
# ============================================================

carica_secret_file()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()


# ============================================================
# VERIFICA CONFIGURAZIONE
# ============================================================

print()
print("TELEGRAM_BOT_TOKEN:", "OK" if TELEGRAM_BOT_TOKEN else "MANCANTE")
print("FOOTBALL_API_KEY:", "OK" if FOOTBALL_API_KEY else "MANCANTE")
print()


# ============================================================
# CREAZIONE BOT TELEGRAM
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN non configurato. "
        "Inseriscilo nelle Environment Variables / Secret Files di Render."
    )

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode="HTML"
)


# ============================================================
# FUNZIONE HTTP GENERICA
# ============================================================

def richiesta_http(url, params=None, headers=None, timeout=API_TIMEOUT):

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
            print("❌ HTTP ERROR:", risposta.text[:500])
            return None

        try:
            return risposta.json()

        except Exception:
            print("❌ Risposta non JSON.")
            return None

    except requests.exceptions.Timeout:
        print("❌ Timeout HTTP.")
        return None

    except requests.exceptions.RequestException as errore:
        print(f"❌ Errore HTTP: {errore}")
        return None

    except Exception as errore:
        print(f"❌ Errore generico HTTP: {errore}")
        return None


# ============================================================
# API-FOOTBALL
# ============================================================

def api_football_get(endpoint, params=None):

    if not FOOTBALL_API_KEY:
        print("⚠️ FOOTBALL_API_KEY non configurata.")
        return None

    chiave_cache = (
        endpoint,
        tuple(sorted((params or {}).items()))
    )

    adesso = time.time()

    if chiave_cache in cache_api:
        timestamp, dati = cache_api[chiave_cache]

        if adesso - timestamp < CACHE_API_SECONDS:
            print("♻️ API-Football: utilizzo cache.")
            return dati

    url = f"{API_FOOTBALL_BASE}/{endpoint}"

    headers = {
        "x-apisports-key": FOOTBALL_API_KEY
    }

    print()
    print("==========================================")
    print("🌐 API-FOOTBALL:", f"/{endpoint}")
    print("📋 PARAMETRI:", params)
    print("==========================================")

    dati = richiesta_http(
        url,
        params=params,
        headers=headers
    )

    if dati is not None:
        cache_api[chiave_cache] = (
            adesso,
            dati
        )

    return dati


# ============================================================
# THESPORTSDB
# ============================================================

def sportsdb_get(endpoint, params=None):

    chiave_cache = (
        endpoint,
        tuple(sorted((params or {}).items()))
    )

    adesso = time.time()

    if chiave_cache in cache_sportsdb:
        timestamp, dati = cache_sportsdb[chiave_cache]

        if adesso - timestamp < CACHE_SPORTSDB_SECONDS:
            print("♻️ TheSportsDB: utilizzo cache.")
            return dati

    url = f"{SPORTSDB_BASE}/{endpoint}"

    print()
    print("==========================================")
    print("🌐 THESPORTSDB:", f"/{endpoint}")
    print("📋 PARAMETRI:", params)
    print("==========================================")

    dati = richiesta_http(
        url,
        params=params
    )

    if dati is not None:
        cache_sportsdb[chiave_cache] = (
            adesso,
            dati
        )

    return dati


# ============================================================
# DATA / STAGIONE
# ============================================================

def stagione_corrente():

    oggi = datetime.now()

    # Campionati europei:
    # stagione 2026-2027 da luglio 2026 in poi.

    if oggi.month >= 7:
        return f"{oggi.year}-{oggi.year + 1}"

    return f"{oggi.year - 1}-{oggi.year}"


def stagione_api_football():

    oggi = datetime.now()

    if oggi.month >= 7:
        return oggi.year

    return oggi.year - 1


def parse_data_evento(data_string):

    if not data_string:
        return None

    formati = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z"
    ]

    for formato in formati:
        try:
            return datetime.strptime(data_string, formato)
        except Exception:
            pass

    return None


def formatta_data(data_string, ora=None):

    data = parse_data_evento(data_string)

    if not data:
        return str(data_string or "")

    giorni = [
        "Lunedì",
        "Martedì",
        "Mercoledì",
        "Giovedì",
        "Venerdì",
        "Sabato",
        "Domenica"
    ]

    giorno = giorni[data.weekday()]

    if ora:
        try:
            ora_pulita = str(ora)[:5]
        except Exception:
            ora_pulita = ""

        if ora_pulita:
            return f"{giorno} {data.strftime('%d/%m/%Y')} ore {ora_pulita}"

    return f"{giorno} {data.strftime('%d/%m/%Y')}"


# ============================================================
# CONVERSIONE EVENTO THESPORTSDB
# ============================================================

def converti_evento_sportsdb(evento):

    if not evento:
        return None

    casa = (
        evento.get("strHomeTeam")
        or evento.get("homeTeam")
        or "Casa"
    )

    trasferta = (
        evento.get("strAwayTeam")
        or evento.get("awayTeam")
        or "Trasferta"
    )

    data = evento.get("dateEvent")

    ora = evento.get("strTime")

    fixture_id = None

    # TheSportsDB può avere il riferimento ad API-Football
    raw_api_id = evento.get("idAPIfootball")

    if raw_api_id:
        try:
            fixture_id = int(raw_api_id)
        except Exception:
            fixture_id = None

    raw_event_id = evento.get("idEvent")

    try:
        event_id = int(raw_event_id) if raw_event_id else None
    except Exception:
        event_id = None

    return {
        "id": fixture_id,
        "sportsdb_id": event_id,
        "home": casa,
        "away": trasferta,
        "date": data,
        "time": ora,
        "status": evento.get("strStatus"),
        "league": evento.get("strLeague"),
        "raw": evento
    }


# ============================================================
# RECUPERA PARTITE DA THESPORTSDB
# ============================================================

def recupera_partite_sportsdb(nome_campionato):

    campionato = CAMPIONATI.get(nome_campionato.lower())

    if not campionato:
        return []

    league_id = campionato["sportsdb_id"]

    stagione = stagione_corrente()

    print()
    print("==========================================")
    print("⚽ THESPORTSDB - CALENDARIO")
    print("🏆 Campionato:", campionato["nome"])
    print("🆔 League ID:", league_id)
    print("📅 Stagione:", stagione)
    print("==========================================")

    # --------------------------------------------------------
    # PRIMO TENTATIVO:
    # EVENTI DELL'INTERA STAGIONE
    # --------------------------------------------------------

    dati = sportsdb_get(
        "eventsseason.php",
        {
            "id": league_id,
            "s": stagione
        }
    )

    eventi = []

    if isinstance(dati, dict):
        eventi = dati.get("events") or []

    print("📦 Eventi ricevuti:", len(eventi))

    # --------------------------------------------------------
    # SE NON ARRIVA NULLA, TENTIAMO EVENTI SUCCESSIVI
    # --------------------------------------------------------

    if not eventi:

        print("⚠️ Nessun evento dalla stagione.")
        print("🔎 Provo eventsnextleague.php...")

        dati_next = sportsdb_get(
            "eventsnextleague.php",
            {
                "id": league_id
            }
        )

        if isinstance(dati_next, dict):
            eventi = dati_next.get("events") or []

        print(
            "📦 Eventi ricevuti da eventsnextleague:",
            len(eventi)
        )

    if not eventi:
        print("❌ TheSportsDB non ha restituito partite.")
        return []

    oggi = datetime.now().date()

    partite = []

    for evento in eventi:

        convertito = converti_evento_sportsdb(evento)

        if not convertito:
            continue

        data_evento = parse_data_evento(
            convertito["date"]
        )

        if not data_evento:
            continue

        # Solo partite da oggi in avanti
        if data_evento.date() < oggi:
            continue

        partite.append(convertito)

    # Ordina per data e ora
    partite.sort(
        key=lambda partita: (
            partita.get("date") or "",
            partita.get("time") or ""
        )
    )

    partite = partite[:NUMERO_PARTITE_REPORT]

    print()
    print("✅ PARTITE FUTURE TROVATE:", len(partite))

    for partita in partite:
        print(
            "⚽",
            partita["home"],
            "-",
            partita["away"],
            "|",
            partita["date"],
            partita["time"]
        )

    return partite


# ============================================================
# FALLBACK API-FOOTBALL
# ============================================================

def recupera_partite_api_football(nome_campionato):

    campionato = CAMPIONATI.get(nome_campionato.lower())

    if not campionato:
        return []

    league_id = campionato["api_id"]

    stagione = stagione_api_football()

    print()
    print("==========================================")
    print("🔎 FALLBACK API-FOOTBALL")
    print("🏆 League ID:", league_id)
    print("📅 Stagione:", stagione)
    print("==========================================")

    dati = api_football_get(
        "fixtures",
        {
            "league": league_id,
            "season": stagione,
            "timezone": "Europe/Rome"
        }
    )

    if not isinstance(dati, dict):
        return []

    errori = dati.get("errors")

    if errori:
        print("❌ Errori API:", errori)
        return []

    risultati = dati.get("response") or []

    oggi = datetime.now().date()

    partite = []

    for item in risultati:

        fixture = item.get("fixture") or {}
        teams = item.get("teams") or {}

        timestamp = fixture.get("timestamp")

        data_string = None

        if timestamp:
            try:
                data_string = datetime.fromtimestamp(
                    timestamp
                ).strftime("%Y-%m-%d")
            except Exception:
                pass

        if not data_string:
            data_ora = fixture.get("date", "")
            data_string = str(data_ora)[:10]

        data_evento = parse_data_evento(data_string)

        if data_evento and data_evento.date() < oggi:
            continue

        casa = (teams.get("home") or {}).get("name")
        trasferta = (teams.get("away") or {}).get("name")

        if not casa or not trasferta:
            continue

        partite.append({
            "id": fixture.get("id"),
            "sportsdb_id": None,
            "home": casa,
            "away": trasferta,
            "date": data_string,
            "time": (
                str(fixture.get("date", ""))[11:16]
                if fixture.get("date")
                else ""
            ),
            "status": (fixture.get("status") or {}).get("short"),
            "league": nome_campionato,
            "raw": item
        })

    partite.sort(
        key=lambda partita: (
            partita.get("date") or "",
            partita.get("time") or ""
        )
    )

    return partite[:NUMERO_PARTITE_REPORT]


# ============================================================
# RECUPERO CALENDARIO PRINCIPALE
# ============================================================

def recupera_partite(nome_campionato):

    print()
    print("==========================================")
    print("⚽ RICERCA PROSSIME PARTITE")
    print("==========================================")

    # --------------------------------------------------------
    # PRIORITÀ 1: THESPORTSDB
    # --------------------------------------------------------

    partite = recupera_partite_sportsdb(
        nome_campionato
    )

    if partite:
        print()
        print("✅ CALENDARIO RECUPERATO DA THESPORTSDB.")
        return partite

    # --------------------------------------------------------
    # PRIORITÀ 2: API-FOOTBALL
    # --------------------------------------------------------

    print()
    print("⚠️ TheSportsDB non ha restituito partite.")
    print("🔎 Provo API-Football come seconda fonte...")

    partite = recupera_partite_api_football(
        nome_campionato
    )

    if partite:
        print()
        print("✅ CALENDARIO RECUPERATO DA API-FOOTBALL.")
        return partite

    # --------------------------------------------------------
    # NESSUNA FONTE DISPONIBILE
    # --------------------------------------------------------

    print()
    print("❌ Nessuna fonte ha restituito il calendario.")

    return []


# ============================================================
# RECUPERO FORMA SQUADRA
# ============================================================

def trova_team_id(nome_squadra):

    if not nome_squadra:
        return None

    dati = api_football_get(
        "teams",
        {
            "search": nome_squadra
        }
    )

    if not isinstance(dati, dict):
        return None

    risultati = dati.get("response") or []

    if not risultati:
        return None

    try:
        return risultati[0]["team"]["id"]
    except Exception:
        return None


def recupera_form_squadra(nome_squadra):

    team_id = trova_team_id(nome_squadra)

    if not team_id:
        return []

    dati = api_football_get(
        "fixtures",
        {
            "team": team_id,
            "last": NUMERO_PARTITE_FORM,
            "status": "FT"
        }
    )

    if not isinstance(dati, dict):
        return []

    if dati.get("errors"):
        print(
            "⚠️ Errore recupero forma:",
            dati.get("errors")
        )
        return []

    risultati = dati.get("response") or []

    forma = []

    for partita in risultati:

        teams = partita.get("teams") or {}
        goals = partita.get("goals") or {}

        casa = teams.get("home") or {}
        trasferta = teams.get("away") or {}

        nome_casa = casa.get("name")
        nome_trasferta = trasferta.get("name")

        gol_casa = goals.get("home")
        gol_trasferta = goals.get("away")

        if gol_casa is None or gol_trasferta is None:
            continue

        if nome_squadra.lower() == str(nome_casa).lower():
            fatti = gol_casa
            subiti = gol_trasferta
            vittoria = gol_casa > gol_trasferta

        else:
            fatti = gol_trasferta
            subiti = gol_casa
            vittoria = gol_trasferta > gol_casa

        pareggio = gol_casa == gol_trasferta

        forma.append({
            "fatti": fatti,
            "subiti": subiti,
            "vittoria": vittoria,
            "pareggio": pareggio
        })

    return forma


# ============================================================
# CALCOLO STATISTICO
# ============================================================

def analizza_form(forma):

    if not forma:
        return {
            "partite": 0,
            "media_fatti": 0,
            "media_subiti": 0,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0
        }

    vittorie = sum(
        1 for x in forma
        if x["vittoria"]
    )

    pareggi = sum(
        1 for x in forma
        if x["pareggio"]
    )

    sconfitte = (
        len(forma)
        - vittorie
        - pareggi
    )

    media_fatti = (
        sum(x["fatti"] for x in forma)
        / len(forma)
    )

    media_subiti = (
        sum(x["subiti"] for x in forma)
        / len(forma)
    )

    return {
        "partite": len(forma),
        "media_fatti": media_fatti,
        "media_subiti": media_subiti,
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte
    }


# ============================================================
# PREVISIONE STATISTICA
# ============================================================

def previsione_statistica(nome_casa, nome_trasferta):

    print()
    print(
        "📊 Analisi statistica:",
        nome_casa,
        "-",
        nome_trasferta
    )

    form_casa = recupera_form_squadra(nome_casa)
    form_trasferta = recupera_form_squadra(nome_trasferta)

    analisi_casa = analizza_form(form_casa)
    analisi_trasferta = analizza_form(form_trasferta)

    if (
        analisi_casa["partite"] == 0
        and analisi_trasferta["partite"] == 0
    ):
        return {
            "esito": "N/D",
            "gol": "N/D",
            "over_under": "N/D",
            "confidence": 0,
            "metodo": "Dati statistici insufficienti"
        }

    forza_casa = (
        analisi_casa["media_fatti"]
        - analisi_casa["media_subiti"]
    )

    forza_trasferta = (
        analisi_trasferta["media_fatti"]
        - analisi_trasferta["media_subiti"]
    )

    # Vantaggio campo
    differenza = forza_casa - forza_trasferta + 0.25

    if differenza > 0.35:
        esito = "1"
    elif differenza < -0.35:
        esito = "2"
    else:
        esito = "X"

    media_gol = (
        analisi_casa["media_fatti"]
        + analisi_trasferta["media_fatti"]
        + analisi_casa["media_subiti"]
        + analisi_trasferta["media_subiti"]
    ) / 2

    if media_gol >= 2.5:
        over_under = "Over 2.5"
    else:
        over_under = "Under 2.5"

    if esito == "1":
        gol = "2-1"
    elif esito == "2":
        gol = "1-2"
    else:
        gol = "1-1"

    partite_disponibili = (
        analisi_casa["partite"]
        + analisi_trasferta["partite"]
    )

    confidence = min(
        85,
        45 + partite_disponibili * 2
    )

    return {
        "esito": esito,
        "gol": gol,
        "over_under": over_under,
        "confidence": confidence,
        "metodo": "Analisi statistica forma recente"
    }


# ============================================================
# PREVISIONE API-FOOTBALL
# ============================================================

def previsione_api_football(fixture_id):

    if not fixture_id:
        return None

    print()
    print(
        "🔮 API-FOOTBALL PREDICTION:",
        fixture_id
    )

    dati = api_football_get(
        "predictions",
        {
            "fixture": fixture_id
        }
    )

    if not isinstance(dati, dict):
        return None

    if dati.get("errors"):
        print(
            "⚠️ Errore predictions:",
            dati.get("errors")
        )
        return None

    risultati = dati.get("response") or []

    if not risultati:
        print("⚠️ Nessuna previsione API-Football.")
        return None

    try:
        prediction = risultati[0].get("predictions") or {}

        vincente = prediction.get("winner") or {}

        nome_vincente = vincente.get("name")

        percentuali = prediction.get("percent") or {}

        home_percent = percentuali.get("home")
        draw_percent = percentuali.get("draw")
        away_percent = percentuali.get("away")

        return {
            "winner": nome_vincente,
            "home_percent": home_percent,
            "draw_percent": draw_percent,
            "away_percent": away_percent,
            "goals": prediction.get("goals"),
            "advice": prediction.get("advice")
        }

    except Exception as errore:
        print(
            "⚠️ Errore parsing prediction:",
            errore
        )
        return None


# ============================================================
# GENERAZIONE PREVISIONE
# ============================================================

def genera_previsione(partita):

    casa = partita.get("home", "Casa")
    trasferta = partita.get("away", "Trasferta")

    fixture_id = partita.get("id")

    # Prima proviamo API-Football
    previsione_api = previsione_api_football(
        fixture_id
    )

    if previsione_api:

        winner = previsione_api.get("winner")

        home_percent = previsione_api.get(
            "home_percent"
        )

        draw_percent = previsione_api.get(
            "draw_percent"
        )

        away_percent = previsione_api.get(
            "away_percent"
        )

        if winner:

            if home_percent is None:
                home_percent = "N/D"

            if draw_percent is None:
                draw_percent = "N/D"

            if away_percent is None:
                away_percent = "N/D"

            advice = (
                previsione_api.get("advice")
                or "Analisi API-Football"
            )

            return {
                "esito": winner,
                "gol": "N/D",
                "over_under": "N/D",
                "confidence": "API",
                "metodo": "API-Football",
                "extra": (
                    f"1: {home_percent} | "
                    f"X: {draw_percent} | "
                    f"2: {away_percent}\n"
                    f"Consiglio: {advice}"
                )
            }

    # --------------------------------------------------------
    # FALLBACK STATISTICO
    # --------------------------------------------------------

    statistica = previsione_statistica(
        casa,
        trasferta
    )

    statistica["extra"] = ""

    return statistica


# ============================================================
# FORMATTA PARTITA
# ============================================================

def formatta_partita(partita, numero):

    casa = partita.get("home", "Casa")
    trasferta = partita.get("away", "Trasferta")

    data = partita.get("date")
    ora = partita.get("time")

    data_formattata = formatta_data(
        data,
        ora
    )

    previsione = genera_previsione(
        partita
    )

    esito = previsione.get(
        "esito",
        "N/D"
    )

    gol = previsione.get(
        "gol",
        "N/D"
    )

    over_under = previsione.get(
        "over_under",
        "N/D"
    )

    confidence = previsione.get(
        "confidence",
        "N/D"
    )

    metodo = previsione.get(
        "metodo",
        ""
    )

    extra = previsione.get(
        "extra",
        ""
    )

    testo = (
        f"<b>{numero}. {casa} - {trasferta}</b>\n"
        f"📅 {data_formattata}\n"
        f"🎯 <b>Pronostico:</b> {esito}\n"
        f"⚽ <b>Risultato:</b> {gol}\n"
        f"📈 <b>Over/Under:</b> {over_under}\n"
        f"📊 <b>Affidabilità:</b> {confidence}\n"
        f"🔎 <b>Metodo:</b> {metodo}"
    )

    if extra:
        testo += f"\n{extra}"

    return testo


# ============================================================
# CREAZIONE REPORT
# ============================================================

def crea_report(nome_campionato):

    campionato = CAMPIONATI.get(
        nome_campionato.lower()
    )

    if not campionato:
        return (
            "❌ Campionato non riconosciuto.\n\n"
            "Usa /campionati per vedere quelli disponibili."
        )

    print()
    print("==========================================")
    print("🚨 CREAZIONE REPORT:", campionato["nome"])
    print("==========================================")

    partite = recupera_partite(
        nome_campionato
    )

    # --------------------------------------------------------
    # NESSUNA PARTITA
    # --------------------------------------------------------

    if not partite:

        report = (
            f"⚽ <b>PRONOSTICI {campionato['nome']}</b>\n\n"
            "❌ Non sono riuscito a recuperare "
            "le prossime partite.\n\n"
            "🔎 Ho controllato:\n"
            "• TheSportsDB\n"
            "• API-Football\n\n"
            "⚠️ API-Football Free limita l'accesso "
            "alla stagione corrente."
        )

        return report

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    righe = []

    righe.append(
        f"⚽ <b>PRONOSTICI {campionato['nome']}</b>"
    )

    righe.append("")

    righe.append(
        "📅 <b>PROSSIME PARTITE</b>"
    )

    righe.append("")

    righe.append(
        "⚠️ Pronostici a scopo statistico."
    )

    righe.append("")

    for indice, partita in enumerate(
        partite,
        start=1
    ):

        print(
            f"🔮 Elaborazione partita "
            f"{indice}/{len(partite)}: "
            f"{partita['home']} - {partita['away']}"
        )

        try:

            testo_partita = formatta_partita(
                partita,
                indice
            )

            righe.append(
                testo_partita
            )

            righe.append("")

        except Exception as errore:

            print(
                "❌ Errore elaborazione partita:",
                errore
            )

            righe.append(
                f"<b>{indice}. "
                f"{partita.get('home')} - "
                f"{partita.get('away')}</b>\n"
                "⚠️ Analisi non disponibile."
            )

            righe.append("")

    righe.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    righe.append(
        "🤖 <b>Bot Pronostici Calcio</b>"
    )

    report = "\n".join(righe)

    print()
    print("📨 Report finale creato.")
    print(
        "📏 Lunghezza report:",
        len(report),
        "caratteri"
    )

    return report


# ============================================================
# INVIO MESSAGGI TELEGRAM
# ============================================================

def invia_report(chat_id, report):

    # Telegram consente messaggi fino a circa 4096 caratteri.
    # Usiamo un margine di sicurezza.

    limite = 3900

    parti = []

    testo = report

    while len(testo) > limite:

        posizione = testo.rfind(
            "\n",
            0,
            limite
        )

        if posizione <= 0:
            posizione = limite

        parti.append(
            testo[:posizione]
        )

        testo = testo[posizione:].lstrip()

    if testo:
        parti.append(testo)

    totale = len(parti)

    for indice, parte in enumerate(
        parti,
        start=1
    ):

        try:

            print(
                f"📤 Invio messaggio "
                f"{indice}/{totale} ..."
            )

            bot.send_message(
                chat_id,
                parte
            )

            print(
                f"✅ Messaggio "
                f"{indice}/{totale} inviato."
            )

        except Exception as errore:

            print(
                f"❌ Errore invio messaggio "
                f"{indice}:",
                errore
            )

    print()
    print("🏁 REPORT TELEGRAM INVIATO COMPLETAMENTE.")


# ============================================================
# MENU TELEGRAM
# ============================================================

def menu_principale():

    return (
        "⚽ <b>BOT PRONOSTICI CALCIO</b>\n\n"
        "Benvenuto!\n\n"
        "Scegli un campionato scrivendo:\n\n"
        "🇮🇹 <b>Serie A</b>\n"
        "🏴 <b>Premier League</b>\n"
        "🇪🇸 <b>La Liga</b>\n"
        "🇩🇪 <b>Bundesliga</b>\n"
        "🇫🇷 <b>Ligue 1</b>\n\n"
        "Oppure usa /campionati."
    )


def elenco_campionati():

    return (
        "🏆 <b>CAMPIONATI DISPONIBILI</b>\n\n"
        "🇮🇹 Serie A\n"
        "🏴 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "Scrivi il nome del campionato "
        "per ricevere i prossimi pronostici."
    )


# ============================================================
# HANDLER /START
# ============================================================

@bot.message_handler(commands=["start"])
def comando_start(message):

    print()
    print(
        f"📩 /start ricevuto da chat "
        f"{message.chat.id}"
    )

    try:

        bot.send_message(
            message.chat.id,
            menu_principale()
        )

    except Exception as errore:

        print(
            "❌ Errore invio /start:",
            errore
        )


# ============================================================
# HANDLER /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def comando_help(message):

    testo = (
        "ℹ️ <b>COME USARE IL BOT</b>\n\n"
        "Scrivi il nome di uno dei campionati "
        "disponibili.\n\n"
        "Esempio:\n"
        "🇮🇹 Serie A\n\n"
        "Il bot recupererà le prossime partite "
        "e preparerà un report statistico."
    )

    try:
        bot.send_message(
            message.chat.id,
            testo
        )

    except Exception as errore:

        print(
            "❌ Errore /help:",
            errore
        )


# ============================================================
# HANDLER /CAMPIONATI
# ============================================================

@bot.message_handler(commands=["campionati"])
def comando_campionati(message):

    try:

        bot.send_message(
            message.chat.id,
            elenco_campionati()
        )

    except Exception as errore:

        print(
            "❌ Errore /campionati:",
            errore
        )


# ============================================================
# NORMALIZZAZIONE NOME CAMPIONATO
# ============================================================

def riconosci_campionato(testo):

    if not testo:
        return None

    testo = testo.lower().strip()

    sostituzioni = {
        "🇮🇹": "",
        "🇬🇧": "",
        "🏴": "",
        "🇪🇸": "",
        "🇩🇪": "",
        "🇫🇷": "",
        "serie a": "serie a",
        "premier": "premier league",
        "premier league": "premier league",
        "la liga": "la liga",
        "liga": "la liga",
        "bundesliga": "bundesliga",
        "ligue1": "ligue 1",
        "ligue 1": "ligue 1"
    }

    for chiave, valore in sostituzioni.items():
        testo = testo.replace(
            chiave,
            valore
        )

    testo = testo.strip()

    if testo in CAMPIONATI:
        return testo

    return None


# ============================================================
# HANDLER MESSAGGI
# ============================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def gestione_messaggio(message):

    testo = message.text or ""

    campionato = riconosci_campionato(
        testo
    )

    if not campionato:

        try:

            bot.send_message(
                message.chat.id,
                "❌ Campionato non riconosciuto.\n\n"
                "Scrivi /campionati per vedere "
                "quelli disponibili."
            )

        except Exception as errore:

            print(
                "❌ Errore risposta:",
                errore
            )

        return

    print()
    print(
        f"📨 RICHIESTA CAMPIONATO: "
        f"{CAMPIONATI[campionato]['nome']}"
    )

    try:

        report = crea_report(
            campionato
        )

        invia_report(
            message.chat.id,
            report
        )

    except Exception as errore:

        print()
        print(
            "❌ ERRORE CREAZIONE REPORT:",
            errore
        )

        try:

            bot.send_message(
                message.chat.id,
                "❌ Si è verificato un errore "
                "durante la creazione del report.\n\n"
                "Riprova tra poco."
            )

        except Exception as errore2:

            print(
                "❌ Errore invio messaggio errore:",
                errore2
            )


# ============================================================
# SERVER HTTP RENDER
# ============================================================

class RenderHandler(
    http.server.BaseHTTPRequestHandler
):

    def log_message(self, format, *args):
        # Evita log HTTP inutili.
        return

    def _invia_risposta(
        self,
        codice=200,
        contenuto="OK"
    ):

        contenuto_bytes = contenuto.encode(
            "utf-8"
        )

        self.send_response(codice)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(contenuto_bytes))
        )

        self.end_headers()

        self.wfile.write(
            contenuto_bytes
        )

    def do_GET(self):

        percorso = urlparse(
            self.path
        ).path

        print()
        print(
            "🌐 GET RICEVUTA:",
            percorso
        )

        if percorso == "/":
            self._invia_risposta(
                200,
                "Bot Pronostici Calcio ONLINE"
            )
            return

        if percorso == "/health":
            self._invia_risposta(
                200,
                "OK"
            )
            return

        if percorso == "/telegram/webhook":
            self._invia_risposta(
                200,
                "Webhook Telegram attivo"
            )
            return

        self._invia_risposta(
            404,
            "Not Found"
        )

    def do_POST(self):

        percorso = urlparse(
            self.path
        ).path

        print()
        print(
            "📩 POST RICEVUTA:",
            percorso
        )

        if percorso != "/telegram/webhook":

            self._invia_risposta(
                404,
                "Not Found"
            )

            return

        try:

            lunghezza = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            print(
                "📩 Dimensione richiesta:",
                lunghezza,
                "byte"
            )

            corpo = self.rfile.read(
                lunghezza
            )

            update_json = json.loads(
                corpo.decode("utf-8")
            )

            print(
                "✅ Update Telegram decodificato."
            )

            self._invia_risposta(
                200,
                "OK"
            )

            print(
                "⚙️ Elaborazione update Telegram..."
            )

            def elabora_update():

                try:

                    update = (
                        telebot.types.Update.de_json(
                            update_json
                        )
                    )

                    bot.process_new_updates(
                        [update]
                    )

                    print(
                        "✅ Update Telegram elaborato."
                    )

                except Exception as errore:

                    print()
                    print(
                        "❌ ERRORE ELABORAZIONE UPDATE:",
                        errore
                    )

            thread = threading.Thread(
                target=elabora_update,
                daemon=True
            )

            thread.start()

            print(
                "✅ Update inviato al thread "
                "di elaborazione."
            )

        except json.JSONDecodeError:

            print(
                "❌ JSON Telegram non valido."
            )

            self._invia_risposta(
                400,
                "Invalid JSON"
            )

        except Exception as errore:

            print(
                "❌ ERRORE WEBHOOK:",
                errore
            )

            try:

                self._invia_risposta(
                    500,
                    "Internal Server Error"
                )

            except Exception:
                pass


# ============================================================
# SERVER HTTP
# ============================================================

def avvia_server_http():

    try:

        server = socketserver.ThreadingTCPServer(
            ("0.0.0.0", RENDER_PORT),
            RenderHandler
        )

        server.allow_reuse_address = True

        print()
        print(
            "=========================================="
        )
        print(
            "🌐 SERVER HTTP RENDER"
        )
        print(
            "=========================================="
        )

        print(
            f"🚀 Server HTTP avviato sulla porta "
            f"{RENDER_PORT}"
        )

        server.serve_forever()

    except Exception as errore:

        print(
            "❌ ERRORE SERVER HTTP:",
            errore
        )


# ============================================================
# CONFIGURAZIONE WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():

    webhook_url = (
        "https://bot-pronostici-gratis.onrender.com"
        "/telegram/webhook"
    )

    print()
    print(
        "=========================================="
    )
    print(
        "🔎 VERIFICA BOT TELEGRAM"
    )
    print(
        "=========================================="
    )

    try:

        info = bot.get_me()

        print(
            "🤖 BOT TELEGRAM:",
            f"@{info.username}"
        )

        print(
            "🆔 BOT ID:",
            info.id
        )

    except Exception as errore:

        print(
            "❌ Impossibile verificare Telegram:",
            errore
        )

        return False

    print()
    print(
        "🔗 Impostazione webhook:"
    )

    print(
        webhook_url
    )

    try:

        # Elimina eventuale webhook precedente.
        bot.delete_webhook(
            drop_pending_updates=False
        )

        time.sleep(1)

        risultato = bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message"]
        )

        print(
            "✅ Webhook impostato:",
            risultato
        )

        time.sleep(1)

        info_webhook = bot.get_webhook_info()

        print()
        print(
            "========== STATO WEBHOOK =========="
        )

        print(
            "URL:",
            info_webhook.url
        )

        print(
            "Pending updates:",
            info_webhook.pending_update_count
        )

        print(
            "Ultimo errore:",
            info_webhook.last_error_message
        )

        print(
            "Data ultimo errore:",
            info_webhook.last_error_date
        )

        print(
            "IP Telegram:",
            info_webhook.ip_address
        )

        print(
            "Max connessioni:",
            info_webhook.max_connections
        )

        print(
            "Allowed updates:",
            info_webhook.allowed_updates
        )

        print(
            "===================================="
        )

        if info_webhook.url == webhook_url:

            print()
            print(
                "✅ WEBHOOK TELEGRAM CONFIGURATO "
                "CORRETTAMENTE."
            )

            return True

        print()
        print(
            "❌ URL WEBHOOK NON CORRISPONDENTE."
        )

        return False

    except Exception as errore:

        print()
        print(
            "❌ ERRORE CONFIGURAZIONE WEBHOOK:",
            errore
        )

        return False


# ============================================================
# AVVIO APPLICAZIONE
# ============================================================

def main():

    print()
    print(
        "=========================================="
    )
    print(
        "⚽ BOT PRONOSTICI CALCIO"
    )
    print(
        "=========================================="
    )

    print(
        "Avvio applicazione Render..."
    )

    print(
        "=========================================="
    )

    # --------------------------------------------------------
    # SERVER HTTP
    # --------------------------------------------------------

    thread_server = threading.Thread(
        target=avvia_server_http,
        daemon=True
    )

    thread_server.start()

    print(
        "Server Render attivo."
    )

    time.sleep(2)

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

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

    configurato = configura_webhook()

    if configurato:

        print(
            "🟢 Telegram configurato correttamente."
        )

    else:

        print(
            "🔴 Telegram NON configurato correttamente."
        )

    # --------------------------------------------------------
    # BOT ONLINE
    # --------------------------------------------------------

    print()
    print(
        "=========================================="
    )

    print(
        "🚀 BOT ONLINE!"
    )

    print(
        "=========================================="
    )

    # --------------------------------------------------------
    # MANTIENI PROCESSO ATTIVO
    # --------------------------------------------------------

    while True:

        time.sleep(60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
