import os
import threading
import http.server
import json
from datetime import datetime, timedelta

import requests
import telebot


# ============================================================
# CONFIGURAZIONE RENDER - SECRET FILE
# ============================================================

SECRET_FILE = "/etc/secrets/bot_secrets.env"


def carica_secret_file():
    """Legge le credenziali dal Secret File di Render."""

    if not os.path.exists(SECRET_FILE):
        print("ATTENZIONE: Secret File non trovato.", flush=True)
        return

    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as file:

            for line in file:
                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)

                key = key.strip()
                value = value.strip()

                if not os.getenv(key):
                    os.environ[key] = value

        print("Secret File caricato correttamente.", flush=True)

    except Exception as e:
        print(
            f"Errore nella lettura del Secret File: {e}",
            flush=True
        )


carica_secret_file()


# ============================================================
# CREDENZIALI
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# URL RENDER
# ============================================================

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://bot-pronostici-gratis.onrender.com"
)

RENDER_EXTERNAL_URL = RENDER_EXTERNAL_URL.rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_URL = (
    RENDER_EXTERNAL_URL
    + WEBHOOK_PATH
)


# ============================================================
# API-FOOTBALL
# ============================================================

API_URL = "https://v3.football.api-sports.io"


# ============================================================
# CONTROLLO CREDENZIALI
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "ERRORE: TELEGRAM_BOT_TOKEN non configurato."
    )

if not FOOTBALL_API_KEY:
    raise RuntimeError(
        "ERRORE: FOOTBALL_API_KEY non configurato."
    )


print("TELEGRAM_BOT_TOKEN: OK", flush=True)
print("FOOTBALL_API_KEY: OK", flush=True)


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN
)


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


# ============================================================
# STAGIONE CORRENTE
# ============================================================

def stagione_corrente():
    """
    API-Football identifica la stagione con l'anno
    in cui inizia il campionato.
    """

    oggi = datetime.now()

    if oggi.month >= 7:
        return oggi.year

    return oggi.year - 1


# ============================================================
# RICHIESTA API-FOOTBALL
# ============================================================

def api_request(endpoint, params=None):
    url = f"{API_URL}/{endpoint}"

    headers = {
        "x-apisports-key": FOOTBALL_API_KEY
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        print("========== API REQUEST ==========", flush=True)
        print(f"URL: {url}", flush=True)
        print(f"Status HTTP: {response.status_code}", flush=True)
        print(f"Risposta API completa: {response.text}", flush=True)
        print("=================================", flush=True)

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:

        print(
            f"❌ ERRORE RICHIESTA API: {e}",
            flush=True
        )

        return None


# ============================================================
# RECUPERA PARTITE
# ============================================================

def recupera_partite(league_id):
    """
    Recupera le prossime partite del campionato.

    Per la Serie A utilizza il calendario ESPN,
    così possiamo ottenere più partite future.
    """

    CAMPIONATI_ESPN = {
        135: "ita.1",   # Serie A
        39: "eng.1",    # Premier League
        140: "esp.1",   # La Liga
        78: "ger.1",    # Bundesliga
        61: "fra.1"     # Ligue 1
    }

    espn_league = CAMPIONATI_ESPN.get(league_id)

    if not espn_league:
        print(
            f"❌ Campionato non configurato ESPN: {league_id}",
            flush=True
        )
        return []

    # Data di oggi
    oggi = datetime.now()

    # Cerchiamo le partite nei prossimi 14 giorni
    data_inizio = oggi.strftime("%Y%m%d")
    data_fine = (
        oggi + timedelta(days=14)
    ).strftime("%Y%m%d")

    url = (
        "https://site.api.espn.com/"
        "apis/site/v2/sports/soccer/"
        f"{espn_league}/scoreboard"
    )

    params = {
        "dates": f"{data_inizio}-{data_fine}"
    }

    print(
        f"📡 ESPN - recupero partite "
        f"{espn_league}",
        flush=True
    )

    print(
        f"📅 Periodo: "
        f"{data_inizio} - {data_fine}",
        flush=True
    )

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        print(
            f"📡 Status HTTP ESPN: "
            f"{response.status_code}",
            flush=True
        )

        response.raise_for_status()

        dati = response.json()

        eventi = dati.get("events") or []

        print(
            f"📊 Eventi ricevuti da ESPN: "
            f"{len(eventi)}",
            flush=True
        )

        if not eventi:

            print(
                "❌ Nessuna partita trovata da ESPN.",
                flush=True
            )

            return []

        partite = []

        for evento in eventi:

            fixture_id = evento.get("id")

            data_partita = evento.get(
                "date"
            )

            nome_partita = evento.get(
                "name",
                ""
            )

            stato = (
                evento
                .get("status", {})
                .get("type", {})
                .get("name", "")
            )

            # Recuperiamo le squadre
            competizioni = evento.get(
                "competitions",
                []
            )

            if not competizioni:
                continue

            competitors = competizioni[0].get(
                "competitors",
                []
            )

            if len(competitors) < 2:
                continue

            casa = "Casa"
            trasferta = "Trasferta"

            for squadra in competitors:

                team = squadra.get(
                    "team",
                    {}
                )

                nome_team = team.get(
                    "displayName",
                    "Squadra"
                )

                home_away = squadra.get(
                    "homeAway"
                )

                if home_away == "home":
                    casa = nome_team

                elif home_away == "away":
                    trasferta = nome_team

            print(
                f"📅 {data_partita} | "
                f"{casa} - {trasferta} | "
                f"ID: {fixture_id} | "
                f"Stato: {stato}",
                flush=True
            )

            if not fixture_id:
                continue

            # Teniamo solamente partite non ancora concluse
            if stato.upper() in [
                "STATUS_FINAL",
                "STATUS_FULL_TIME",
                "STATUS_FINAL_PEN"
            ]:
                continue

            partite.append({
                "id": fixture_id,
                "casa": casa,
                "trasferta": trasferta,
                "data": data_partita
            })

        # Ordina per data
        partite.sort(
            key=lambda x: x.get("data") or ""
        )

        # Elimina duplicati
        partite_uniche = []
        ids_visti = set()

        for partita in partite:

            if partita["id"] in ids_visti:
                continue

            ids_visti.add(
                partita["id"]
            )

            partite_uniche.append(
                partita
            )

        # Massimo 8 partite
        partite_uniche = partite_uniche[:8]

        print(
            f"✅ Prossime partite trovate: "
            f"{len(partite_uniche)}",
            flush=True
        )

        return partite_uniche

    except requests.exceptions.RequestException as e:

        print(
            f"❌ Errore HTTP ESPN: {e}",
            flush=True
        )

        return []

    except ValueError as e:

        print(
            f"❌ Errore JSON ESPN: {e}",
            flush=True
        )

        return []

    except Exception as e:

        print(
            f"❌ Errore recupero partite ESPN: {e}",
            flush=True
        )

        return []

@bot.message_handler(commands=["testdb"])
def comando_testdb(message):

    print(
        "🧪 COMANDO /TESTDB RICEVUTO",
        flush=True
    )

    partite = recupera_partite_thesportsdb(135)

    if not partite:

        bot.send_message(
            message.chat.id,
            "❌ TheSportsDB non ha restituito partite."
        )

        return

    testo = "🧪 <b>TEST THESPORTSDB</b>\n\n"

    for partita in partite[:10]:

        testo += (
            f"📅 {partita['data']}\n"
            f"🏠 {partita['casa']}\n"
            f"✈️ {partita['trasferta']}\n\n"
        )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )

def recupera_form_squadra(nome_squadra, giorni=60):
    """
    Recupera i risultati recenti di una squadra
    tramite ESPN e calcola il rendimento.
    """

    oggi = datetime.now()

    data_inizio = (
        oggi - timedelta(days=giorni)
    ).strftime("%Y%m%d")

    data_fine = oggi.strftime("%Y%m%d")

    url = (
        "https://site.api.espn.com/"
        "apis/site/v2/sports/soccer/"
        "ita.1/scoreboard"
    )

    params = {
        "dates": f"{data_inizio}-{data_fine}"
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        dati = response.json()

        eventi = dati.get("events") or []

        risultati = []

        for evento in eventi:

            competizioni = evento.get(
                "competitions",
                []
            )

            if not competizioni:
                continue

            competitors = competizioni[0].get(
                "competitors",
                []
            )

            if len(competitors) != 2:
                continue

            squadra_trovata = False
            squadra = None
            avversaria = None

            for c in competitors:

                team = c.get("team", {})
                nome = team.get(
                    "displayName",
                    ""
                )

                if nome.lower() == nome_squadra.lower():

                    squadra = c
                    squadra_trovata = True

                else:

                    avversaria = c

            if not squadra_trovata:
                continue

            stato = (
                evento
                .get("status", {})
                .get("type", {})
                .get("name", "")
            )

            if stato not in [
                "STATUS_FINAL",
                "STATUS_FINAL_PEN",
                "STATUS_FULL_TIME"
            ]:
                continue

            try:

                gol_squadra = int(
                    squadra.get("score", 0)
                )

                gol_avversaria = int(
                    avversaria.get("score", 0)
                )

            except Exception:

                continue

            casa_trasferta = squadra.get(
                "homeAway"
            )

            if gol_squadra > gol_avversaria:
                risultato = "V"

            elif gol_squadra == gol_avversaria:
                risultato = "N"

            else:
                risultato = "P"

            risultati.append({
                "risultato": risultato,
                "gol_fatti": gol_squadra,
                "gol_subiti": gol_avversaria,
                "casa": casa_trasferta == "home",
                "data": evento.get("date", "")
            })

        risultati.sort(
            key=lambda x: x.get("data", ""),
            reverse=True
        )

        return risultati[:10]

    except Exception as e:

        print(
            f"⚠️ Errore form {nome_squadra}: {e}",
            flush=True
        )

        return []


def calcola_statistiche_squadra(
    nome_squadra,
    partite
):
    """
    Calcola le statistiche recenti
    della squadra.
    """

    if not partite:
        return {
            "partite": 0,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0,
            "gol_fatti": 0,
            "gol_subiti": 0,
            "media_gol_fatti": 0,
            "media_gol_subiti": 0,
            "punti_media": 0,
            "forma": ""
        }

    vittorie = sum(
        1 for p in partite
        if p["risultato"] == "V"
    )

    pareggi = sum(
        1 for p in partite
        if p["risultato"] == "N"
    )

    sconfitte = sum(
        1 for p in partite
        if p["risultato"] == "P"
    )

    gol_fatti = sum(
        p["gol_fatti"]
        for p in partite
    )

    gol_subiti = sum(
        p["gol_subiti"]
        for p in partite
    )

    numero_partite = len(partite)

    media_gol_fatti = (
        gol_fatti / numero_partite
    )

    media_gol_subiti = (
        gol_subiti / numero_partite
    )

    punti = (
        vittorie * 3
        + pareggi
    )

    punti_media = (
        punti / numero_partite
    )

    forma = "".join(
        p["risultato"]
        for p in partite[:5]
    )

    return {
        "partite": numero_partite,
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte,
        "gol_fatti": gol_fatti,
        "gol_subiti": gol_subiti,
        "media_gol_fatti": media_gol_fatti,
        "media_gol_subiti": media_gol_subiti,
        "punti_media": punti_media,
        "forma": forma
    }


def genera_pronostico_statistico(
    casa,
    trasferta
):
    """
    Genera un pronostico statistico
    basato sulla forma recente delle due squadre.
    """

    print(
        f"📊 Analisi statistica: "
        f"{casa} - {trasferta}",
        flush=True
    )

    form_casa = recupera_form_squadra(casa)
    form_trasferta = recupera_form_squadra(
        trasferta
    )

    stats_casa = calcola_statistiche_squadra(
        casa,
        form_casa
    )

    stats_trasferta = calcola_statistiche_squadra(
        trasferta,
        form_trasferta
    )

    print(
        f"🏠 {casa}: "
        f"{stats_casa['forma']} | "
        f"GF {stats_casa['media_gol_fatti']:.2f} | "
        f"GS {stats_casa['media_gol_subiti']:.2f}",
        flush=True
    )

    print(
        f"✈️ {trasferta}: "
        f"{stats_trasferta['forma']} | "
        f"GF {stats_trasferta['media_gol_fatti']:.2f} | "
        f"GS {stats_trasferta['media_gol_subiti']:.2f}",
        flush=True
    )

    # Se non abbiamo dati sufficienti
    if (
        stats_casa["partite"] == 0
        or stats_trasferta["partite"] == 0
    ):

        return {
            "esito": "N/D",
            "over25": "N/D",
            "gol": "N/D",
            "confidence": 0,
            "motivazione": (
                "Dati statistici insufficienti "
                "per elaborare il pronostico."
            )
        }

    # Punteggio di forza
    forza_casa = (
        stats_casa["punti_media"] * 10
        + stats_casa["media_gol_fatti"] * 3
        - stats_casa["media_gol_subiti"] * 2
    )

    forza_trasferta = (
        stats_trasferta["punti_media"] * 10
        + stats_trasferta["media_gol_fatti"] * 3
        - stats_trasferta["media_gol_subiti"] * 2
    )

    # Bonus per il fattore campo
    forza_casa += 2

    differenza = (
        forza_casa - forza_trasferta
    )

    # -----------------------------
    # ESITO
    # -----------------------------

    if differenza >= 3:

        esito = "1"

    elif differenza >= 1:

        esito = "1X"

    elif differenza <= -3:

        esito = "2"

    elif differenza <= -1:

        esito = "X2"

    else:

        esito = "X"

    # -----------------------------
    # OVER / UNDER
    # -----------------------------

    media_gol_totale = (
        stats_casa["media_gol_fatti"]
        + stats_casa["media_gol_subiti"]
        + stats_trasferta["media_gol_fatti"]
        + stats_trasferta["media_gol_subiti"]
    ) / 2

    if media_gol_totale >= 2.7:

        over25 = "OVER 2.5"

    elif media_gol_totale >= 2.2:

        over25 = "OVER 1.5"

    else:

        over25 = "UNDER 2.5"

    # -----------------------------
    # GOL / NO GOL
    # -----------------------------

    media_casa = (
        stats_casa["media_gol_fatti"]
    )

    media_trasferta = (
        stats_trasferta["media_gol_fatti"]
    )

    if (
        media_casa >= 1.1
        and media_trasferta >= 1.0
    ):

        gol = "GOL"

    else:

        gol = "NO GOL"

    # -----------------------------
    # AFFIDABILITÀ
    # -----------------------------

    confidence = 50

    if abs(differenza) >= 3:
        confidence += 15

    elif abs(differenza) >= 1.5:
        confidence += 10

    if media_gol_totale >= 2.5:
        confidence += 5

    if (
        stats_casa["partite"] >= 5
        and stats_trasferta["partite"] >= 5
    ):
        confidence += 5

    confidence = min(
        confidence,
        85
    )

    # -----------------------------
    # MOTIVAZIONE
    # -----------------------------

    motivazione = (
        f"{casa} nelle ultime "
        f"{stats_casa['partite']} partite: "
        f"{stats_casa['vittorie']} vittorie, "
        f"{stats_casa['pareggi']} pareggi, "
        f"{stats_casa['sconfitte']} sconfitte. "
        f"Media gol fatti "
        f"{stats_casa['media_gol_fatti']:.2f}. "
        f"{trasferta} nelle ultime "
        f"{stats_trasferta['partite']} partite: "
        f"{stats_trasferta['vittorie']} vittorie, "
        f"{stats_trasferta['pareggi']} pareggi, "
        f"{stats_trasferta['sconfitte']} sconfitte. "
        f"Media gol fatti "
        f"{stats_trasferta['media_gol_fatti']:.2f}."
    )

    return {
        "esito": esito,
        "over25": over25,
        "gol": gol,
        "confidence": confidence,
        "motivazione": motivazione
    }
    
# ============================================================
# RECUPERA PRONOSTICO
# ============================================================

def recupera_pronostico(fixture_id):
    """
    Recupera il pronostico statistico
    della partita ESPN.
    """

    print(
        f"🔮 GENERAZIONE PRONOSTICO "
        f"EVENTO ESPN: {fixture_id}",
        flush=True
    )

    # Recuperiamo i dati dell'evento
    url = (
        "https://site.api.espn.com/"
        "apis/site/v2/sports/soccer/"
        "ita.1/summary"
    )

    params = {
        "event": fixture_id
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        dati = response.json()

        evento = dati.get("header", {})

        competizioni = evento.get(
            "competitions",
            []
        )

        if not competizioni:

            print(
                "❌ Nessuna competizione "
                "trovata per l'evento.",
                flush=True
            )

            return None

        competitors = competizioni[0].get(
            "competitors",
            []
        )

        if len(competitors) < 2:

            return None

        casa = None
        trasferta = None

        for squadra in competitors:

            team = squadra.get(
                "team",
                {}
            )

            nome = team.get(
                "displayName"
            )

            if squadra.get("homeAway") == "home":
                casa = nome

            elif squadra.get("homeAway") == "away":
                trasferta = nome

        if not casa or not trasferta:

            print(
                "❌ Impossibile identificare "
                "le squadre.",
                flush=True
            )

            return None

        print(
            f"🏠 CASA: {casa}",
            flush=True
        )

        print(
            f"✈️ TRASFERTA: {trasferta}",
            flush=True
        )

        pronostico = genera_pronostico_statistico(
            casa,
            trasferta
        )

        print(
            f"✅ Esito: "
            f"{pronostico.get('esito')}",
            flush=True
        )

        print(
            f"📈 Over/Under: "
            f"{pronostico.get('over25')}",
            flush=True
        )

        print(
            f"⚽ Gol/No Gol: "
            f"{pronostico.get('gol')}",
            flush=True
        )

        print(
            f"🎯 Affidabilità: "
            f"{pronostico.get('confidence')}%",
            flush=True
        )

        return pronostico

    except requests.exceptions.RequestException as e:

        print(
            f"❌ Errore HTTP ESPN pronostico: {e}",
            flush=True
        )

        return None

    except ValueError as e:

        print(
            f"❌ Errore JSON ESPN pronostico: {e}",
            flush=True
        )

        return None

    except Exception as e:

        print(
            f"❌ Errore pronostico: {e}",
            flush=True
        )

        return None


# ============================================================
# FORMATTA DATA
# ============================================================

def formatta_data(data_string):
    """Converte la data API in formato italiano."""

    if not data_string:
        return "Data non disponibile"

    try:

        data = datetime.fromisoformat(
            data_string.replace(
                "Z",
                "+00:00"
            )
        )

        return data.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return data_string

# ============================================================
# FORMATTA PRONOSTICO
# ============================================================

def formatta_pronostico(pronostico):
    """
    Formatta il pronostico per Telegram.
    """

    if not pronostico:

        return (
            "📊 <b>Pronostico non disponibile.</b>"
        )

    esito = pronostico.get(
        "esito",
        "N/D"
    )

    over25 = pronostico.get(
        "over25",
        "N/D"
    )

    gol = pronostico.get(
        "gol",
        "N/D"
    )

    confidence = pronostico.get(
        "confidence",
        0
    )

    motivazione = pronostico.get(
        "motivazione",
        ""
    )

    risultato = (
        "🔮 <b>PRONOSTICO</b>\n\n"
        f"🎯 <b>Esito:</b> {esito}\n"
        f"⚽ <b>Gol:</b> {gol}\n"
        f"📈 <b>Totale gol:</b> {over25}\n"
        f"💯 <b>Affidabilità:</b> {confidence}%"
    )

    if motivazione:

        risultato += (
            f"\n\n💡 <b>Analisi:</b>\n"
            f"{motivazione}"
        )

    return risultato

# ============================================================
# CREA REPORT
# ============================================================

def crea_report(nome_campionato, league_id):
    """
    Crea il messaggio Telegram con le prossime partite
    e i relativi pronostici.
    """

    print(
        f"🚨 CREAREPORT CHIAMATO: {nome_campionato} | ID: {league_id}",
        flush=True
    )

    print(
        f"Recupero partite: {nome_campionato}",
        flush=True
    )

    print(
        "🚨 STO PER CHIAMARE recupera_partite()",
        flush=True
    )

    partite = recupera_partite(
        league_id
    )

    if not partite:

        return (
            f"⚽ {nome_campionato}\n\n"
            "❌ Non sono state trovate partite "
            "nei prossimi 14 giorni."
        )

    messaggio = [
        f"⚽ <b>{nome_campionato}</b>",
        "",
        "🔮 <b>PRONOSTICI</b>",
        ""
    ]

    for partita in partite[:8]:

        fixture_id = partita["id"]

        data = formatta_data(
            partita["data"]
        )

        messaggio.append(
            f"📅 <b>{data}</b>\n"
            f"🏠 {partita['casa']}\n"
            f"✈️ {partita['trasferta']}"
        )

        print(
            f"Recupero pronostico fixture {fixture_id}",
            flush=True
        )

        pronostico = recupera_pronostico(
            fixture_id
        )

        messaggio.append(
            formatta_pronostico(
                pronostico
            )
        )

        messaggio.append(
            "━━━━━━━━━━━━━━━━━━"
        )

    return "\n".join(messaggio)


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def comando_start(message):

    print(
        f"📩 Comando /start ricevuto da chat "
        f"{message.chat.id}",
        flush=True
    )

    testo = (
        "⚽ <b>BOT PRONOSTICI CALCIO</b>\n\n"
        "Benvenuto! 👋\n\n"
        "Posso mostrarti le prossime partite "
        "dei principali campionati e i relativi "
        "pronostici.\n\n"
        "📋 Usa /campionati per scegliere "
        "il campionato.\n\n"
        "❓ Usa /help per vedere i comandi disponibili."
    )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(
    commands=["help"]
)
def comando_help(message):

    testo = (
        "📖 <b>COMANDI DISPONIBILI</b>\n\n"
        "/start - Avvia il bot\n"
        "/campionati - Mostra i campionati\n"
        "/help - Mostra questo messaggio\n\n"
        "Puoi anche scrivere direttamente "
        "il nome del campionato."
    )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )


# ============================================================
# /CAMPIONATI
# ============================================================

@bot.message_handler(
    commands=["campionati"]
)
def comando_campionati(message):

    testo = (
        "🏆 <b>SCEGLI IL CAMPIONATO</b>\n\n"
        "🇮🇹 Serie A\n"
        "🇬🇧 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "Scrivi il nome del campionato "
        "che vuoi analizzare."
    )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )


# ============================================================
# GESTIONE MESSAGGI
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def gestione_messaggio(message):

    if not message.text:
        return

    testo_utente = (
        message.text.strip().lower()
    )

    campionato_trovato = None
    league_id = None

    for nome, id_campionato in CAMPIONATI.items():

        nome_pulito = (
            nome.replace("🇮🇹", "")
                .replace("🇬🇧", "")
                .replace("🇪🇸", "")
                .replace("🇩🇪", "")
                .replace("🇫🇷", "")
                .strip()
                .lower()
        )

        if testo_utente == nome_pulito:

            campionato_trovato = nome
            league_id = id_campionato

            break

    if not campionato_trovato:

        bot.send_message(
            message.chat.id,
            "❌ Campionato non riconosciuto.\n\n"
            "Usa /campionati per vedere quelli disponibili."
        )

        return

    messaggio_attesa = bot.send_message(
        message.chat.id,
        f"⏳ Sto analizzando "
        f"<b>{campionato_trovato}</b>...\n\n"
        "Attendi qualche secondo.",
        parse_mode="HTML"
    )

    try:

        report = crea_report(
            campionato_trovato,
            league_id
        )

        if len(report) > 4000:

            report = (
                report[:4000]
                + "\n\n…"
            )

        bot.send_message(
            message.chat.id,
            report,
            parse_mode="HTML"
        )

        try:

            bot.delete_message(
                message.chat.id,
                messaggio_attesa.message_id
            )

        except Exception:

            pass

    except Exception as e:

        print(
            f"Errore durante la creazione del report: {e}",
            flush=True
        )

        bot.send_message(
            message.chat.id,
            "❌ Si è verificato un errore durante "
            "il recupero dei pronostici."
        )


# ============================================================
# SERVER HTTP RENDER + WEBHOOK TELEGRAM
# ============================================================

class HealthHandler(
    http.server.BaseHTTPRequestHandler
):

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(self):

        print(
            f"🌐 GET ricevuta: {self.path}",
            flush=True
        )

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Bot pronostici calcio online!"
        )

    # --------------------------------------------------------
    # POST WEBHOOK
    # --------------------------------------------------------

    def do_POST(self):

        print(
            f"📩 POST RICEVUTA: {self.path}",
            flush=True
        )

        if self.path != WEBHOOK_PATH:

            print(
                f"❌ Percorso webhook non corretto: "
                f"{self.path}",
                flush=True
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
                f"{content_length} byte",
                flush=True
            )

            body = self.rfile.read(
                content_length
            )

            print(
                "📩 Dati Telegram ricevuti.",
                flush=True
            )

            data = json.loads(
                body.decode("utf-8")
            )

            update = (
                telebot.types.Update
                .de_json(data)
            )

            print(
                "✅ Update Telegram decodificato.",
                flush=True
            )

            # Rispondi subito a Telegram
            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(
                b"OK"
            )

            # Elaborazione in background
            thread = threading.Thread(
                target=elabora_update,
                args=(update,),
                daemon=True
            )

            thread.start()

            print(
                "✅ Update inviato al thread di elaborazione.",
                flush=True
            )

        except Exception as e:

            print(
                f"❌ ERRORE WEBHOOK: {e}",
                flush=True
            )

            try:

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "text/plain; charset=utf-8"
                )

                self.end_headers()

                self.wfile.write(
                    b"ERROR"
                )

            except Exception:

                pass

    # --------------------------------------------------------
    # LOG HTTP
    # --------------------------------------------------------

    def log_message(
        self,
        format,
        *args
    ):

        return


# ============================================================
# ELABORA UPDATE TELEGRAM
# ============================================================

def elabora_update(update):

    try:

        print(
            "⚙️ Elaborazione update Telegram...",
            flush=True
        )

        bot.process_new_updates(
            [update]
        )

        print(
            "✅ Update Telegram elaborato.",
            flush=True
        )

    except Exception as e:

        print(
            f"❌ Errore elaborazione update: {e}",
            flush=True
        )


# ============================================================
# AVVIO SERVER HTTP
# ============================================================

def avvia_server():

    server = http.server.ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    print(
        f"🌐 Server HTTP avviato sulla porta {PORT}",
        flush=True
    )

    print(
        f"🔗 Webhook Telegram: {WEBHOOK_URL}",
        flush=True
    )

    server.serve_forever()


# ============================================================
# CONFIGURA WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():
    try:
        print("==========================================", flush=True)
        print("CONFIGURAZIONE TELEGRAM", flush=True)

        # Controlla quale bot stiamo utilizzando
        me = bot.get_me()

        print(f"🤖 BOT TELEGRAM: @{me.username}", flush=True)
        print(f"🆔 BOT ID: {me.id}", flush=True)

        # Imposta il webhook
        risultato = bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message"]
        )

        print(f"Webhook impostato: {risultato}", flush=True)
        print(f"URL webhook: {WEBHOOK_URL}", flush=True)

        # Legge lo stato reale dal server Telegram
        info = bot.get_webhook_info()

        print("========== STATO WEBHOOK ==========", flush=True)
        print(f"URL: {info.url}", flush=True)
        print(f"Pending updates: {info.pending_update_count}", flush=True)
        print(f"Ultimo errore: {info.last_error_message}", flush=True)
        print(f"Data ultimo errore: {info.last_error_date}", flush=True)
        print(f"IP Telegram: {info.ip_address}", flush=True)
        print(f"Max connessioni: {info.max_connections}", flush=True)
        print(f"Allowed updates: {info.allowed_updates}", flush=True)
        print("====================================", flush=True)

        return True

    except Exception as e:
        print(f"❌ ERRORE TELEGRAM: {e}", flush=True)
        return False

# ============================================================
# AVVIO PRINCIPALE
# ============================================================

if __name__ == "__main__":

    print(
        "==========================================",
        flush=True
    )

    print(
        "⚽ BOT PRONOSTICI CALCIO",
        flush=True
    )

    print(
        "Avvio applicazione Render...",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    # --------------------------------------------------------
    # SERVER HTTP
    # --------------------------------------------------------

    thread_server = threading.Thread(
        target=avvia_server,
        daemon=True
    )

    thread_server.start()

    print(
        "Server Render attivo.",
        flush=True
    )

    # --------------------------------------------------------
    # WEBHOOK TELEGRAM
    # --------------------------------------------------------

    configura_webhook()

    print(
        "Telegram configurato correttamente.",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    print(
        "✅ BOT ONLINE!",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    # --------------------------------------------------------
    # MANTIENE VIVO IL PROCESSO
    # --------------------------------------------------------

    try:

        while True:

            threading.Event().wait(3600)

    except KeyboardInterrupt:

        print(
            "Arresto bot...",
            flush=True
        )
        
