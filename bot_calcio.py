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
    Recupera le prossime 8 partite del campionato
    utilizzando TheSportsDB.
    """

    CAMPIONATI_THESPORTSDB = {
        135: 4332,  # Serie A
        39: 4328,   # Premier League
        140: 4335,  # La Liga
        78: 4331,   # Bundesliga
        61: 4334    # Ligue 1
    }

    thesportsdb_league_id = CAMPIONATI_THESPORTSDB.get(league_id)

    if not thesportsdb_league_id:
        print(
            f"❌ Campionato non configurato: {league_id}",
            flush=True
        )
        return []

    url = (
        "https://www.thesportsdb.com/"
        "api/v1/json/123/eventsseason.php"
    )

    params = {
        "id": thesportsdb_league_id,
        "s": "2026-2027"
    }

    print(
        f"📡 TheSportsDB - recupero partite "
        f"campionato ID {thesportsdb_league_id}",
        flush=True
    )

    try:
        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        print(
            f"📡 Status HTTP: {response.status_code}",
            flush=True
        )

        response.raise_for_status()

        dati = response.json()

        eventi = dati.get("events") or []

        print(
            f"📊 Eventi ricevuti da TheSportsDB: "
            f"{len(eventi)}",
            flush=True
        )

        if not eventi:
            print(
                "❌ Nessuna partita trovata.",
                flush=True
            )

            print(
                f"Risposta API: {dati}",
                flush=True
            )

            return []

        oggi = datetime.now()

        partite = []

        for evento in eventi:

            fixture_id = evento.get("idEvent")

            casa = evento.get(
                "strHomeTeam",
                "Casa"
            )

            trasferta = evento.get(
                "strAwayTeam",
                "Trasferta"
            )

            data_partita = evento.get(
                "strTimestamp"
            )

            # Se strTimestamp non è disponibile,
            # utilizziamo data + ora locale
            if not data_partita:

                data_evento = evento.get(
                    "dateEvent"
                )

                ora_evento = evento.get(
                    "strTimeLocal"
                )

                if data_evento:

                    data_partita = data_evento

                    if ora_evento:
                        data_partita = (
                            f"{data_evento}T"
                            f"{ora_evento}"
                        )

            if not fixture_id or not data_partita:
                continue

            # Proviamo a trasformare la data
            # in un oggetto datetime
            try:

                data_testo = str(
                    data_partita
                ).replace("Z", "")

                data_gara = datetime.fromisoformat(
                    data_testo
                )

            except Exception:

                try:

                    data_gara = datetime.strptime(
                        str(data_partita)[:10],
                        "%Y-%m-%d"
                    )

                except Exception:

                    print(
                        f"⚠️ Data non valida: "
                        f"{data_partita}",
                        flush=True
                    )

                    continue

            # Ignora le partite già disputate
            if data_gara < oggi:
                continue

            stato = evento.get(
                "strStatus",
                ""
            )

            # Ignora eventuali partite già concluse
            if str(stato).upper() in [
                "FT",
                "AET",
                "PEN",
                "CANC",
                "POST"
            ]:
                continue

            partita = {
                "id": fixture_id,
                "casa": casa,
                "trasferta": trasferta,
                "data": data_partita
            }

            partite.append(partita)

            print(
                f"📅 {data_partita} | "
                f"{casa} - {trasferta} | "
                f"ID: {fixture_id}",
                flush=True
            )

        # Ordina le partite dalla più vicina
        # alla più lontana
        partite.sort(
            key=lambda x: x.get("data") or ""
        )

        # Elimina eventuali duplicati
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

        # Prendiamo al massimo 8 partite
        partite_uniche = partite_uniche[:8]

        print(
            f"✅ Prossime partite trovate: "
            f"{len(partite_uniche)}",
            flush=True
        )

        return partite_uniche

    except requests.exceptions.RequestException as e:

        print(
            f"❌ Errore HTTP TheSportsDB: {e}",
            flush=True
        )

        return []

    except ValueError as e:

        print(
            f"❌ Errore JSON TheSportsDB: {e}",
            flush=True
        )

        return []

    except Exception as e:

        print(
            f"❌ Errore recupero partite: {e}",
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

# ============================================================
# RECUPERA PRONOSTICO
# ============================================================

# ============================================================
# RECUPERA PRONOSTICO
# ============================================================

def recupera_pronostico(fixture_id):
    """
    Genera il pronostico per una partita TheSportsDB.

    NOTA:
    fixture_id è l'ID dell'evento TheSportsDB.

    Questa versione NON utilizza API-Football.
    """

    print(
        f"🔮 GENERAZIONE PRONOSTICO "
        f"EVENTO THESPORTSDB: {fixture_id}",
        flush=True
    )

    pronostico = {
        "esito": "1X",
        "over25": "OVER 1.5",
        "gol": "DA VALUTARE",
        "confidence": 50,
        "motivazione": (
            "Pronostico preliminare. "
            "Analisi statistica dettagliata "
            "non ancora disponibile."
        )
    }

    print(
        f"✅ Esito: {pronostico['esito']}",
        flush=True
    )

    print(
        f"📈 Over/Under: {pronostico['over25']}",
        flush=True
    )

    print(
        f"⚽ Gol/No Gol: {pronostico['gol']}",
        flush=True
    )

    print(
        f"🎯 Affidabilità: "
        f"{pronostico['confidence']}%",
        flush=True
    )

    return pronostico


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
        
