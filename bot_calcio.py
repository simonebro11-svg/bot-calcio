import os
import threading
import http.server
from datetime import datetime, timedelta

import requests
import telebot


# ============================================================
# CONFIGURAZIONE RENDER - SECRET FILE
# ============================================================

SECRET_FILE = "/etc/secrets/bot_secrets.env"


def carica_secret_file():
    """
    Legge le credenziali dal Secret File di Render.
    """

    if not os.path.exists(SECRET_FILE):
        print("ATTENZIONE: Secret File non trovato.")
        return

    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()

                # Ignora righe vuote e commenti
                if not line or line.startswith("#"):
                    continue

                # Deve esserci il carattere =
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)

                key = key.strip()
                value = value.strip()

                # Inserisce la variabile solo se non esiste già
                if not os.getenv(key):
                    os.environ[key] = value

        print("Secret File caricato correttamente.")

    except Exception as e:
        print(f"Errore nella lettura del Secret File: {e}")


carica_secret_file()


# ============================================================
# CREDENZIALI
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")


# Porta assegnata automaticamente da Render
PORT = int(os.getenv("PORT", "10000"))


# API-Football
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


# Non stampiamo mai le chiavi nei log
print("TELEGRAM_BOT_TOKEN: OK")
print("FOOTBALL_API_KEY: OK")


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


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

    Esempio:
    settembre 2026 -> stagione 2026
    gennaio 2027 -> stagione 2026
    """

    oggi = datetime.now()

    if oggi.month >= 7:
        return oggi.year

    return oggi.year - 1


# ============================================================
# RICHIESTA API
# ============================================================

def api_request(endpoint, params):
    """
    Effettua una richiesta ad API-Football.
    """

    headers = {
        "x-apisports-key": FOOTBALL_API_KEY
    }

    url = f"{API_URL}/{endpoint}"

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:
        print(f"Errore API-Football: {e}")
        return None

    except ValueError as e:
        print(f"Errore risposta JSON: {e}")
        return None


# ============================================================
# RECUPERA PARTITE
# ============================================================

def recupera_partite(league_id):
    """
    Recupera le partite dei prossimi 14 giorni.
    """

    oggi = datetime.now().date()
    data_fine = oggi + timedelta(days=14)

    stagione = stagione_corrente()

    params = {
        "league": league_id,
        "season": stagione,
        "from": oggi.strftime("%Y-%m-%d"),
        "to": data_fine.strftime("%Y-%m-%d"),
        "timezone": "Europe/Rome"
    }

    dati = api_request("fixtures", params)

    if not dati:
        return []

    partite = []

    for item in dati.get("response", []):

        fixture = item.get("fixture", {})
        teams = item.get("teams", {})

        status = fixture.get("status", {}).get("short")

        # Solo partite non ancora iniziate
        if status not in ["NS", "TBD"]:
            continue

        casa = teams.get("home", {}).get("name", "Casa")
        trasferta = teams.get("away", {}).get("name", "Trasferta")

        data_partita = fixture.get("date")

        partite.append({
            "id": fixture.get("id"),
            "casa": casa,
            "trasferta": trasferta,
            "data": data_partita
        })

    # Ordina per data
    partite.sort(key=lambda x: x.get("data") or "")

    return partite


# ============================================================
# RECUPERA PRONOSTICO
# ============================================================

def recupera_pronostico(fixture_id):
    """
    Recupera il pronostico di API-Football.
    """

    dati = api_request(
        "predictions",
        {
            "fixture": fixture_id
        }
    )

    if not dati:
        return None

    response = dati.get("response", [])

    if not response:
        return None

    return response[0]


# ============================================================
# FORMATTA DATA
# ============================================================

def formatta_data(data_string):
    """
    Converte la data API in formato italiano.
    """

    if not data_string:
        return "Data non disponibile"

    try:
        data = datetime.fromisoformat(
            data_string.replace("Z", "+00:00")
        )

        return data.strftime("%d/%m/%Y %H:%M")

    except Exception:
        return data_string


# ============================================================
# FORMATTA PRONOSTICO
# ============================================================

def formatta_pronostico(pronostico):
    """
    Estrae le informazioni principali dal pronostico.
    """

    if not pronostico:
        return (
            "📊 Pronostico non disponibile."
        )

    predictions = pronostico.get("predictions", {})

    vincitore = predictions.get("winner", {})

    squadra_vincente = vincitore.get("name")

    commento = predictions.get("advice")

    percentuali = predictions.get("percent")

    risultato = []

    if squadra_vincente:
        risultato.append(
            f"🏆 Favorita: {squadra_vincente}"
        )

    if percentuali:
        casa = percentuali.get("home")
        pareggio = percentuali.get("draw")
        trasferta = percentuali.get("away")

        if casa or pareggio or trasferta:
            risultato.append(
                f"📈 Probabilità:\n"
                f"🏠 Casa: {casa}\n"
                f"🤝 X: {pareggio}\n"
                f"✈️ Trasferta: {trasferta}"
            )

    if commento:
        risultato.append(
            f"💡 Consiglio: {commento}"
        )

    if not risultato:
        return "📊 Pronostico non disponibile."

    return "\n".join(risultato)


# ============================================================
# COSTRUISCE REPORT
# ============================================================

def crea_report(nome_campionato, league_id):
    """
    Crea il messaggio Telegram con le prossime partite
    e i relativi pronostici.
    """

    print(f"Recupero partite: {nome_campionato}")

    partite = recupera_partite(league_id)

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

    # Limitiamo il numero di partite per evitare
    # messaggi Telegram troppo lunghi
    for partita in partite[:8]:

        fixture_id = partita["id"]

        data = formatta_data(partita["data"])

        messaggio.append(
            f"📅 <b>{data}</b>\n"
            f"🏠 {partita['casa']}\n"
            f"✈️ {partita['trasferta']}"
        )

        print(
            f"Recupero pronostico fixture {fixture_id}"
        )

        pronostico = recupera_pronostico(fixture_id)

        messaggio.append(
            formatta_pronostico(pronostico)
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

@bot.message_handler(commands=["help"])
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

@bot.message_handler(commands=["campionati"])
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
# GESTIONE CAMPIONATI
# ============================================================

@bot.message_handler(func=lambda message: True)
def gestione_messaggio(message):

    testo_utente = message.text.strip().lower()

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

    # Messaggio temporaneo
    messaggio_attesa = bot.send_message(
        message.chat.id,
        f"⏳ Sto analizzando <b>{campionato_trovato}</b>...\n\n"
        "Attendi qualche secondo.",
        parse_mode="HTML"
    )

    try:

        report = crea_report(
            campionato_trovato,
            league_id
        )

        # Telegram ha un limite di circa 4096 caratteri
        if len(report) > 4000:
            report = report[:4000] + "\n\n…"

        bot.send_message(
            message.chat.id,
            report,
            parse_mode="HTML"
        )

        # Cancella il messaggio di attesa
        try:
            bot.delete_message(
                message.chat.id,
                messaggio_attesa.message_id
            )
        except Exception:
            pass

    except Exception as e:

        print(f"Errore durante la creazione del report: {e}")

        bot.send_message(
            message.chat.id,
            "❌ Si è verificato un errore durante "
            "il recupero dei pronostici."
        )


# ============================================================
# SERVER HTTP PER RENDER
# ============================================================

class HealthHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)
        self.send_header(
            "Content-type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

        self.wfile.write(
            b"Bot pronostici calcio online!"
        )

    def log_message(self, format, *args):
        # Evita log HTTP inutili
        return


def avvia_server():

    server = http.server.HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(
        f"Server HTTP avviato sulla porta {PORT}"
    )

    server.serve_forever()


# ============================================================
# AVVIO BOT
# ============================================================

if __name__ == "__main__":

    # Avvia server HTTP in background
    thread_server = threading.Thread(
        target=avvia_server,
        daemon=True
    )

    thread_server.start()

    print("Server Render attivo.")
    print("Avvio Telegram bot...")

    try:

        # Rimuove eventuale webhook precedente
        bot.remove_webhook()

        # Avvia polling
        bot.infinity_polling(
            skip_pending=True,
            timeout=60,
            long_polling_timeout=60
        )

    except Exception as e:

        print(f"Errore Telegram bot: {e}")
        raise
