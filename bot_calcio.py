import os
import threading
import http.server
import json
from datetime import datetime, timedelta, timezone

import requests
import telebot


# ============================================================
# CONFIGURAZIONE RENDER - SECRET FILE
# ============================================================

SECRET_FILE = "/etc/secrets/bot_secrets.env"


def carica_secret_file():
    """Legge le credenziali dal Secret File di Render."""
    if not os.path.exists(SECRET_FILE):
        print("⚠️ Secret File non trovato.", flush=True)
        return

    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()

                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if not os.getenv(key):
                    os.environ[key] = value

        print("✅ Secret File caricato correttamente.", flush=True)

    except Exception as e:
        print(f"❌ Errore lettura Secret File: {e}", flush=True)


carica_secret_file()


# ============================================================
# CREDENZIALI / RENDER
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")  # mantenuta per compatibilità

PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://bot-pronostici-gratis.onrender.com"
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = RENDER_EXTERNAL_URL + WEBHOOK_PATH


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("ERRORE: TELEGRAM_BOT_TOKEN non configurato.")

print("TELEGRAM_BOT_TOKEN: OK", flush=True)

if FOOTBALL_API_KEY:
    print("FOOTBALL_API_KEY: OK", flush=True)
else:
    print("ℹ️ FOOTBALL_API_KEY non utilizzata per calendario/pronostici ESPN.", flush=True)


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


# ============================================================
# CAMPIONATI
# API-Football ID -> ESPN league
# ============================================================

CAMPIONATI = {
    "🇮🇹 Serie A": 135,
    "🇬🇧 Premier League": 39,
    "🇪🇸 La Liga": 140,
    "🇩🇪 Bundesliga": 78,
    "🇫🇷 Ligue 1": 61,
}

CAMPIONATI_ESPN = {
    135: "ita.1",
    39: "eng.1",
    140: "esp.1",
    78: "ger.1",
    61: "fra.1",
}


# ============================================================
# CONFIGURAZIONE MODELLO
# ============================================================

GIORNI_PARTITE_FUTURE = 14
GIORNI_FORM = 90
NUMERO_PARTITE_REPORT = 8
NUMERO_PARTITE_FORMA = 10


# ============================================================
# FUNZIONI UTILI
# ============================================================

def get_espn_league(league_id):
    return CAMPIONATI_ESPN.get(league_id)


def parse_data(data_string):
    if not data_string:
        return None

    try:
        return datetime.fromisoformat(
            data_string.replace("Z", "+00:00")
        )
    except Exception:
        return None


def formatta_data(data_string):
    if not data_string:
        return "Data non disponibile"

    data = parse_data(data_string)

    if not data:
        return data_string

    # Conversione semplice UTC -> ora italiana.
    # In caso di errore lasciamo l'orario originale.
    try:
        if data.tzinfo is not None:
            from zoneinfo import ZoneInfo
            data = data.astimezone(ZoneInfo("Europe/Rome"))

        return data.strftime("%d/%m/%Y %H:%M")

    except Exception:
        return data.strftime("%d/%m/%Y %H:%M")


def nome_squadra_da_competitor(competitor):
    return (
        competitor.get("team", {}).get("displayName")
        or competitor.get("team", {}).get("shortDisplayName")
        or "Squadra"
    )


# ============================================================
# RICHIESTA ESPN GENERICA
# ============================================================

def richiesta_espn(url, params=None):
    try:
        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as e:
        print(f"❌ Errore HTTP ESPN: {e}", flush=True)
        return None

    except ValueError as e:
        print(f"❌ Errore JSON ESPN: {e}", flush=True)
        return None


# ============================================================
# RECUPERA PARTITE FUTURE
# ============================================================

def recupera_partite(league_id):
    """
    Recupera le prossime partite del campionato da ESPN.

    Funziona per tutti i campionati presenti in CAMPIONATI_ESPN.
    """

    espn_league = get_espn_league(league_id)

    if not espn_league:
        print(
            f"❌ Campionato non configurato ESPN: {league_id}",
            flush=True
        )
        return []

    oggi = datetime.now()
    data_inizio = oggi.strftime("%Y%m%d")
    data_fine = (
        oggi + timedelta(days=GIORNI_PARTITE_FUTURE)
    ).strftime("%Y%m%d")

    url = (
        "https://site.api.espn.com/"
        "apis/site/v2/sports/soccer/"
        f"{espn_league}/scoreboard"
    )

    params = {
        "dates": f"{data_inizio}-{data_fine}"
    }

    print("==========================================", flush=True)
    print("⚽ RICERCA PROSSIME PARTITE", flush=True)
    print(f"🏆 League ID: {league_id}", flush=True)
    print(f"📡 ESPN League: {espn_league}", flush=True)
    print(
        f"📅 Periodo: {data_inizio} - {data_fine}",
        flush=True
    )
    print("==========================================", flush=True)

    dati = richiesta_espn(url, params)

    if not dati:
        return []

    eventi = dati.get("events") or []

    print(
        f"📊 Eventi ricevuti da ESPN: {len(eventi)}",
        flush=True
    )

    partite = []
    ids_visti = set()

    for evento in eventi:
        fixture_id = str(evento.get("id") or "").strip()
        data_partita = evento.get("date")

        stato = (
            evento.get("status", {})
            .get("type", {})
            .get("name", "")
        )

        competizioni = evento.get("competitions") or []

        if not fixture_id or not competizioni:
            continue

        competitors = competizioni[0].get("competitors") or []

        if len(competitors) < 2:
            continue

        casa = None
        trasferta = None

        for competitor in competitors:
            nome = nome_squadra_da_competitor(competitor)

            if competitor.get("homeAway") == "home":
                casa = nome
            elif competitor.get("homeAway") == "away":
                trasferta = nome

        if not casa or not trasferta:
            continue

        print(
            f"📅 {data_partita} | {casa} - {trasferta} | "
            f"ID: {fixture_id} | Stato: {stato}",
            flush=True
        )

        stato_upper = str(stato).upper()

        if any(
            parola in stato_upper
            for parola in (
                "FINAL",
                "FULL_TIME",
                "POSTPONED",
                "CANCELED",
                "CANCELLED"
            )
        ):
            continue

        if fixture_id in ids_visti:
            continue

        ids_visti.add(fixture_id)

        partite.append({
            "id": fixture_id,
            "casa": casa,
            "trasferta": trasferta,
            "data": data_partita,
            "league_id": league_id,
            "espn_league": espn_league
        })

    partite.sort(
        key=lambda x: x.get("data") or ""
    )

    partite = partite[:NUMERO_PARTITE_REPORT]

    print(
        f"✅ Prossime partite trovate: {len(partite)}",
        flush=True
    )

    return partite


# ============================================================
# RECUPERA FORMA RECENTE DI UNA SQUADRA
# ============================================================

def recupera_form_squadra(
    nome_squadra,
    espn_league,
    giorni=GIORNI_FORM
):
    """
    Recupera le ultime partite della squadra nello stesso campionato.
    Restituisce solo partite concluse.
    """

    oggi = datetime.now()

    data_inizio = (
        oggi - timedelta(days=giorni)
    ).strftime("%Y%m%d")

    data_fine = oggi.strftime("%Y%m%d")

    url = (
        "https://site.api.espn.com/"
        "apis/site/v2/sports/soccer/"
        f"{espn_league}/scoreboard"
    )

    params = {
        "dates": f"{data_inizio}-{data_fine}"
    }

    dati = richiesta_espn(url, params)

    if not dati:
        return []

    eventi = dati.get("events") or []
    risultati = []

    nome_cercato = nome_squadra.strip().lower()

    for evento in eventi:
        stato = (
            evento.get("status", {})
            .get("type", {})
            .get("name", "")
        )

        stato_upper = str(stato).upper()

        if not any(
            parola in stato_upper
            for parola in (
                "FINAL",
                "FULL_TIME"
            )
        ):
            continue

        competizioni = evento.get("competitions") or []

        if not competizioni:
            continue

        competitors = competizioni[0].get("competitors") or []

        if len(competitors) < 2:
            continue

        squadra = None
        avversaria = None

        for competitor in competitors:
            nome = nome_squadra_da_competitor(competitor)

            if nome.strip().lower() == nome_cercato:
                squadra = competitor
            else:
                avversaria = competitor

        if squadra is None or avversaria is None:
            continue

        try:
            gol_squadra = int(
                str(squadra.get("score", 0)).split(".")[0]
            )
            gol_avversaria = int(
                str(avversaria.get("score", 0)).split(".")[0]
            )
        except Exception:
            continue

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
            "casa": squadra.get("homeAway") == "home",
            "data": evento.get("date", "")
        })

    risultati.sort(
        key=lambda x: x.get("data", ""),
        reverse=True
    )

    return risultati[:NUMERO_PARTITE_FORMA]


# ============================================================
# STATISTICHE
# ============================================================

def calcola_statistiche_squadra(partite):
    if not partite:
        return {
            "partite": 0,
            "vittorie": 0,
            "pareggi": 0,
            "sconfitte": 0,
            "gol_fatti": 0,
            "gol_subiti": 0,
            "media_gol_fatti": 0.0,
            "media_gol_subiti": 0.0,
            "punti_media": 0.0,
            "forma": "",
            "over15": 0,
            "over25": 0,
            "gol": 0
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
        p["gol_fatti"] for p in partite
    )

    gol_subiti = sum(
        p["gol_subiti"] for p in partite
    )

    over15 = sum(
        1 for p in partite
        if p["gol_fatti"] + p["gol_subiti"] >= 2
    )

    over25 = sum(
        1 for p in partite
        if p["gol_fatti"] + p["gol_subiti"] >= 3
    )

    gol = sum(
        1 for p in partite
        if p["gol_fatti"] > 0 and p["gol_subiti"] > 0
    )

    numero = len(partite)

    punk = vittorie * 3 + pareggi

    return {
        "partite": numero,
        "vittorie": vittorie,
        "pareggi": pareggi,
        "sconfitte": sconfitte,
        "gol_fatti": gol_fatti,
        "gol_subiti": gol_subiti,
        "media_gol_fatti": gol_fatti / numero,
        "media_gol_subiti": gol_subiti / numero,
        "punti_media": punk / numero,
        "forma": "".join(
            p["risultato"] for p in partite[:5]
        ),
        "over15": over15,
        "over25": over25,
        "gol": gol
    }


# ============================================================
# PRONOSTICO STATISTICO
# ============================================================

def genera_pronostico_statistico(
    casa,
    trasferta,
    espn_league
):
    """
    Pronostico statistico basato su:
    - ultime 10 partite di campionato;
    - media punti;
    - media gol fatti/subiti;
    - frequenza Over 1.5 / Over 2.5;
    - frequenza GOL;
    - fattore campo.

    Non è una garanzia del risultato.
    """

    print(
        f"📊 Analisi: {casa} - {trasferta}",
        flush=True
    )

    form_casa = recupera_form_squadra(
        casa,
        espn_league
    )

    form_trasferta = recupera_form_squadra(
        trasferta,
        espn_league
    )

    stats_casa = calcola_statistiche_squadra(
        form_casa
    )

    stats_trasferta = calcola_statistiche_squadra(
        form_trasferta
    )

    print(
        f"🏠 {casa}: {stats_casa['forma']} | "
        f"GF {stats_casa['media_gol_fatti']:.2f} | "
        f"GS {stats_casa['media_gol_subiti']:.2f}",
        flush=True
    )

    print(
        f"✈️ {trasferta}: {stats_trasferta['forma']} | "
        f"GF {stats_trasferta['media_gol_fatti']:.2f} | "
        f"GS {stats_trasferta['media_gol_subiti']:.2f}",
        flush=True
    )

    if (
        stats_casa["partite"] == 0
        or stats_trasferta["partite"] == 0
    ):
        if stats_casa["partite"] == 0 and stats_trasferta["partite"] == 0:
            esito = "1X"
        elif stats_casa["partite"] == 0:
            esito = "X2"
        else:
            esito = "1X"

        return {
            "esito": esito,
            "over25": "OVER 1.5",
            "gol": "NO GOL",
            "confidence": 30,
            "motivazione": (
                "Dati recenti limitati: il modello ha "
                "ridotto automaticamente l'affidabilita."
            )
        }

    # --------------------------------------------------------
    # FORZA DELLE SQUADRE
    # --------------------------------------------------------

    forza_casa = (
        stats_casa["punti_media"] * 9
        + stats_casa["media_gol_fatti"] * 3
        - stats_casa["media_gol_subiti"] * 2
    )

    forza_trasferta = (
        stats_trasferta["punti_media"] * 9
        + stats_trasferta["media_gol_fatti"] * 3
        - stats_trasferta["media_gol_subiti"] * 2
    )

    # Fattore campo moderato.
    forza_casa += 1.5

    differenza = forza_casa - forza_trasferta

    # --------------------------------------------------------
    # ESITO 1X2 / DOPPIA CHANCE
    # --------------------------------------------------------

    if differenza >= 3.0:
        esito = "1"
    elif differenza >= 1.0:
        esito = "1X"
    elif differenza <= -3.0:
        esito = "2"
    elif differenza <= -1.0:
        esito = "X2"
    else:
        esito = "X"

    # --------------------------------------------------------
    # OVER / UNDER
    # --------------------------------------------------------

    media_gol_totali = (
        stats_casa["media_gol_fatti"]
        + stats_casa["media_gol_subiti"]
        + stats_trasferta["media_gol_fatti"]
        + stats_trasferta["media_gol_subiti"]
    ) / 2

    frequenza_over25 = (
        (
            stats_casa["over25"] / stats_casa["partite"]
        )
        +
        (
            stats_trasferta["over25"] / stats_trasferta["partite"]
        )
    ) / 2

    if (
        media_gol_totali >= 2.7
        and frequenza_over25 >= 0.50
    ):
        over25 = "OVER 2.5"
    elif media_gol_totali >= 1.9:
        over25 = "OVER 1.5"
    else:
        over25 = "UNDER 2.5"

    # --------------------------------------------------------
    # GOL / NO GOL
    # --------------------------------------------------------

    frequenza_gol = (
        (
            stats_casa["gol"] / stats_casa["partite"]
        )
        +
        (
            stats_trasferta["gol"] / stats_trasferta["partite"]
        )
    ) / 2

    if frequenza_gol >= 0.55:
        gol = "GOL"
    else:
        gol = "NO GOL"

    # --------------------------------------------------------
    # AFFIDABILITÀ DEL MODELLO
    # --------------------------------------------------------

    confidence = 50

    if abs(differenza) >= 3:
        confidence += 15
    elif abs(differenza) >= 1.5:
        confidence += 10

    if (
        stats_casa["partite"] >= 8
        and stats_trasferta["partite"] >= 8
    ):
        confidence += 5

    if abs(differenza) >= 4.5:
        confidence += 5

    if (
        over25 == "OVER 2.5"
        and frequenza_over25 >= 0.65
    ):
        confidence += 5

    confidence = min(confidence, 85)

    # --------------------------------------------------------
    # MOTIVAZIONE SINTETICA
    # --------------------------------------------------------

    motivazione = (
        f"Forma {casa}: {stats_casa['forma'] or 'N/D'}; "
        f"{trasferta}: {stats_trasferta['forma'] or 'N/D'}. "
        f"Media gol {media_gol_totali:.2f}."
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

def recupera_pronostico(
    fixture_id,
    casa,
    trasferta,
    league_id
):
    """
    Genera il pronostico della partita.
    Non usa più ita.1 fisso: il campionato viene passato
    dinamicamente, quindi funziona anche per Premier, Liga,
    Bundesliga e Ligue 1.
    """

    espn_league = get_espn_league(league_id)

    print(
        f"🔮 GENERAZIONE PRONOSTICO ESPN: {fixture_id}",
        flush=True
    )

    print(
        f"🏆 Campionato ESPN: {espn_league}",
        flush=True
    )

    if not espn_league:
        return None

    try:
        pronostico = genera_pronostico_statistico(
            casa,
            trasferta,
            espn_league
        )

        print(
            f"✅ Esito: {pronostico.get('esito')}",
            flush=True
        )

        print(
            f"📈 Over/Under: {pronostico.get('over25')}",
            flush=True
        )

        print(
            f"⚽ Gol/No Gol: {pronostico.get('gol')}",
            flush=True
        )

        print(
            f"🎯 Affidabilità modello: "
            f"{pronostico.get('confidence')}%",
            flush=True
        )

        return pronostico

    except Exception as e:
        print(
            f"❌ Errore pronostico {fixture_id}: {e}",
            flush=True
        )
        return None


# ============================================================
# FORMATTA PRONOSTICO - VERSIONE SINTETICA
# ============================================================

def formatta_pronostico(pronostico):
    if not pronostico:
        return "🔮 <b>Pronostico non disponibile.</b>"

    esito = pronostico.get("esito", "N/D")
    over25 = pronostico.get("over25", "N/D")
    gol = pronostico.get("gol", "N/D")
    confidence = pronostico.get("confidence", 0)

    return (
        "🔮 <b>PRONOSTICO</b>\n"
        f"🎯 <b>Esito:</b> {esito}\n"
        f"⚽ <b>Gol:</b> {gol}\n"
        f"📈 <b>Totale:</b> {over25}\n"
        f"💯 <b>Affidabilità modello:</b> {confidence}%"
    )


# ============================================================
# CREA REPORT
# ============================================================

def crea_report(nome_campionato, league_id):
    print(
        f"🚨 CREAREPORT: {nome_campionato} | ID: {league_id}",
        flush=True
    )

    partite = recupera_partite(league_id)

    if not partite:
        return (
            f"⚽ <b>{nome_campionato}</b>\n\n"
            "❌ Nessuna partita trovata nei prossimi "
            f"{GIORNI_PARTITE_FUTURE} giorni."
        )

    messaggio = [
        f"⚽ <b>{nome_campionato}</b>",
        "",
        "🔮 <b>PRONOSTICI STATISTICI</b>",
        ""
    ]

    for partita in partite:
        fixture_id = partita["id"]
        casa = partita["casa"]
        trasferta = partita["trasferta"]

        data = formatta_data(
            partita["data"]
        )

        print(
            f"➡️ Pronostico {fixture_id}: "
            f"{casa} - {trasferta}",
            flush=True
        )

        pronostico = recupera_pronostico(
            fixture_id,
            casa,
            trasferta,
            league_id
        )

        messaggio.append(
            f"📅 <b>{data}</b>\n"
            f"🏠 {casa} - {trasferta}\n"
            f"{formatta_pronostico(pronostico)}"
        )

        messaggio.append(
            "━━━━━━━━━━━━━━━━━━"
        )

    return "\n".join(messaggio)


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def comando_start(message):
    print(
        f"📩 /start da chat {message.chat.id}",
        flush=True
    )

    testo = (
        "⚽ <b>BOT PRONOSTICI CALCIO</b>\n\n"
        "Benvenuto! 👋\n\n"
        "Posso analizzare le prossime partite e "
        "generare pronostici statistici.\n\n"
        "📋 Usa /campionati per scegliere il campionato.\n"
        "❓ Usa /help per i comandi."
    )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def comando_help(message):
    testo = (
        "📖 <b>COMANDI</b>\n\n"
        "/start - Avvia il bot\n"
        "/campionati - Scegli il campionato\n"
        "/help - Mostra i comandi\n\n"
        "Scrivi anche direttamente:\n"
        "Serie A\n"
        "Premier League\n"
        "La Liga\n"
        "Bundesliga\n"
        "Ligue 1"
    )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )


# ============================================================
# /CAMPIONATI
# ============================================================

@bot.message_handler(commands=["campionati"])
def comando_campionati(message):
    testo = (
        "🏆 <b>SCEGLI IL CAMPIONATO</b>\n\n"
        "🇮🇹 Serie A\n"
        "🇬🇧 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "Scrivi il nome del campionato."
    )

    bot.send_message(
        message.chat.id,
        testo,
        parse_mode="HTML"
    )


# ============================================================
# GESTIONE MESSAGGI
# ============================================================

@bot.message_handler(func=lambda message: True)
def gestione_messaggio(message):
    if not message.text:
        return

    testo_utente = (
        message.text.strip()
        .lower()
        .replace("🇮🇹", "")
        .replace("🇬🇧", "")
        .replace("🇪🇸", "")
        .replace("🇩🇪", "")
        .replace("🇫🇷", "")
        .strip()
    )

    campionato_trovato = None
    league_id = None

    for nome, id_campionato in CAMPIONATI.items():
        nome_pulito = (
            nome
            .replace("🇮🇹", "")
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
            "Usa /campionati."
        )
        return

    messaggio_attesa = bot.send_message(
        message.chat.id,
        f"⏳ Analizzo <b>{campionato_trovato}</b>...\n"
        "Attendi qualche secondo.",
        parse_mode="HTML"
    )

    try:
        report = crea_report(
            campionato_trovato,
            league_id
        )

        # Telegram consente circa 4096 caratteri.
        # Dividiamo automaticamente il report in più messaggi.
        blocchi = []

        while len(report) > 3900:
            posizione = report.rfind(
                "\n━━━━━━━━━━━━━━━━━━",
                0,
                3900
            )

            if posizione == -1:
                posizione = 3900

            blocchi.append(
                report[:posizione]
            )

            report = report[posizione:].lstrip()

        if report:
            blocchi.append(report)

        for blocco in blocchi:
            bot.send_message(
                message.chat.id,
                blocco,
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
            f"❌ Errore creazione report: {e}",
            flush=True
        )

        bot.send_message(
            message.chat.id,
            "❌ Si è verificato un errore durante "
            "il recupero dei pronostici."
        )


# ============================================================
# SERVER HTTP RENDER + WEBHOOK
# ============================================================

class HealthHandler(
    http.server.BaseHTTPRequestHandler
):

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

    def do_POST(self):
        print(
            f"📩 POST RICEVUTA: {self.path}",
            flush=True
        )

        if self.path != WEBHOOK_PATH:
            print(
                f"❌ Percorso non corretto: {self.path}",
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

            data = json.loads(
                body.decode("utf-8")
            )

            update = telebot.types.Update.de_json(
                data
            )

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )
            self.end_headers()
            self.wfile.write(b"OK")

            thread = threading.Thread(
                target=elabora_update,
                args=(update,),
                daemon=True
            )

            thread.start()

            print(
                "✅ Update inviato al thread.",
                flush=True
            )

        except Exception as e:
            print(
                f"❌ ERRORE WEBHOOK: {e}",
                flush=True
            )

            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ERROR")
            except Exception:
                pass

    def log_message(self, format, *args):
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
# SERVER
# ============================================================

def avvia_server():
    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", PORT),
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
# WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():
    try:
        print(
            "==========================================",
            flush=True
        )

        print(
            "CONFIGURAZIONE TELEGRAM",
            flush=True
        )

        me = bot.get_me()

        print(
            f"🤖 BOT TELEGRAM: @{me.username}",
            flush=True
        )

        print(
            f"🆔 BOT ID: {me.id}",
            flush=True
        )

        risultato = bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message"]
        )

        print(
            f"Webhook impostato: {risultato}",
            flush=True
        )

        info = bot.get_webhook_info()

        print(
            "========== STATO WEBHOOK ==========",
            flush=True
        )

        print(
            f"URL: {info.url}",
            flush=True
        )

        print(
            f"Pending updates: {info.pending_update_count}",
            flush=True
        )

        print(
            f"Ultimo errore: {info.last_error_message}",
            flush=True
        )

        print(
            f"Data ultimo errore: {info.last_error_date}",
            flush=True
        )

        print(
            f"IP Telegram: {info.ip_address}",
            flush=True
        )

        print(
            f"Max connessioni: {info.max_connections}",
            flush=True
        )

        print(
            f"Allowed updates: {info.allowed_updates}",
            flush=True
        )

        print(
            "====================================",
            flush=True
        )

        return True

    except Exception as e:
        print(
            f"❌ ERRORE TELEGRAM: {e}",
            flush=True
        )
        return False


# ============================================================
# AVVIO
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

    thread_server = threading.Thread(
        target=avvia_server,
        daemon=True
    )

    thread_server.start()

    print(
        "Server Render attivo.",
        flush=True
    )

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

    try:
        while True:
            threading.Event().wait(3600)

    except KeyboardInterrupt:
        print(
            "Arresto bot...",
            flush=True
        )
