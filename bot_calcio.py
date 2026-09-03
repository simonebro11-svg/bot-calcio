import os
import threading
import http.server
from datetime import datetime, timedelta

import requests
import telebot


# ============================================================
# CONFIGURAZIONE
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

API_URL = "https://v3.football.api-sports.io"


# Controllo variabili ambiente
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "ERRORE: TELEGRAM_BOT_TOKEN non configurato su Render."
    )

if not FOOTBALL_API_KEY:
    raise RuntimeError(
        "ERRORE: FOOTBALL_API_KEY non configurata su Render."
    )


# ============================================================
# BOT TELEGRAM
# ============================================================

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


# ============================================================
# CAMPIONATI API-FOOTBALL
# ============================================================

CAMPIONATI = {
    "serie a": {
        "league_id": 135,
        "nome": "🇮🇹 SERIE A"
    },

    "premier league": {
        "league_id": 39,
        "nome": "🏴 PREMIER LEAGUE"
    },

    "la liga": {
        "league_id": 140,
        "nome": "🇪🇸 LA LIGA"
    },

    "bundesliga": {
        "league_id": 78,
        "nome": "🇩🇪 BUNDESLIGA"
    },

    "ligue 1": {
        "league_id": 61,
        "nome": "🇫🇷 LIGUE 1"
    }
}


# ============================================================
# SESSIONE API
# ============================================================

session = requests.Session()

session.headers.update({
    "x-apisports-key": FOOTBALL_API_KEY,
    "Accept": "application/json"
})


# ============================================================
# FUNZIONE CHIAMATA API
# ============================================================

def api_get(endpoint, params=None):

    url = API_URL + endpoint

    try:

        response = session.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        # Controllo eventuali errori API
        errors = data.get("errors")

        if errors:

            if isinstance(errors, dict):
                error_text = ", ".join(
                    f"{k}: {v}"
                    for k, v in errors.items()
                )
            else:
                error_text = str(errors)

            raise RuntimeError(
                f"API-Football: {error_text}"
            )

        return data

    except requests.exceptions.Timeout:

        raise RuntimeError(
            "Timeout durante la connessione ad API-Football."
        )

    except requests.exceptions.RequestException as e:

        raise RuntimeError(
            f"Errore connessione API-Football: {e}"
        )


# ============================================================
# STAGIONE CORRENTE
# ============================================================

def stagione_corrente():

    # API-Football identifica la stagione con
    # l'anno di inizio.
    #
    # Esempio:
    # stagione 2025/2026 = 2025
    #
    # A settembre 2026 siamo nella stagione 2026/2027.

    oggi = datetime.now()

    if oggi.month >= 7:
        return oggi.year

    return oggi.year - 1


# ============================================================
# RECUPERA PARTITE FUTURE
# ============================================================

def recupera_partite(league_id):

    stagione = stagione_corrente()

    oggi = datetime.now().date()

    data_fine = oggi + timedelta(days=14)

    params = {
        "league": league_id,
        "season": stagione,
        "from": oggi.strftime("%Y-%m-%d"),
        "to": data_fine.strftime("%Y-%m-%d"),
        "timezone": "Europe/Rome"
    }

    data = api_get(
        "/fixtures",
        params
    )

    partite = data.get("response", [])

    # Ordina per data
    partite.sort(
        key=lambda partita:
        partita.get("fixture", {}).get("timestamp", 0)
    )

    # Consideriamo solamente partite non ancora iniziate
    partite_future = []

    for partita in partite:

        status = (
            partita
            .get("fixture", {})
            .get("status", {})
            .get("short", "")
        )

        if status in ["NS", "TBD"]:
            partite_future.append(partita)

    return partite_future


# ============================================================
# RECUPERA PRONOSTICO API-FOOTBALL
# ============================================================

def recupera_pronostico(fixture_id):

    params = {
        "fixture": fixture_id
    }

    data = api_get(
        "/predictions",
        params
    )

    response = data.get("response", [])

    if not response:
        return None

    return response[0]


# ============================================================
# CONVERSIONE PERCENTUALE
# ============================================================

def percentuale(valore):

    if valore is None:
        return "N/D"

    try:
        return f"{float(valore):.0f}%"
    except:
        return str(valore)


# ============================================================
# ESTRAZIONE PRONOSTICO
# ============================================================

def analizza_pronostico(prediction):

    if not prediction:
        return {
            "winner": "N/D",
            "home": "N/D",
            "draw": "N/D",
            "away": "N/D",
            "goals": "N/D",
            "advice": "N/D"
        }

    predictions = prediction.get(
        "predictions",
        {}
    )

    winner = predictions.get(
        "winner",
        {}
    )

    winner_name = winner.get(
        "name",
        "N/D"
    )

    percent = predictions.get(
        "percent",
        {}
    )

    home = percent.get(
        "home",
        "N/D"
    )

    draw = percent.get(
        "draw",
        "N/D"
    )

    away = percent.get(
        "away",
        "N/D"
    )

    goals = predictions.get(
        "goals",
        {}
    )

    advice = predictions.get(
        "advice",
        "N/D"
    )

    return {
        "winner": winner_name,
        "home": home,
        "draw": draw,
        "away": away,
        "goals": goals,
        "advice": advice
    }


# ============================================================
# FORMATTA DATA
# ============================================================

def formatta_data(data_string):

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

    except:

        return data_string


# ============================================================
# /START E /HELP
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def invia_benvenuto(message):

    testo = (
        "🤖 *BOT PRONOSTICI CALCIO AI* ⚽\n\n"
        "Benvenuto!\n\n"
        "Il bot recupera automaticamente "
        "le partite dai dati di API-Football.\n\n"
        "🏆 *Campionati disponibili:*\n\n"
        "🇮🇹 Serie A\n"
        "🏴 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "✍️ Scrivi il nome del campionato.\n\n"
        "Esempio:\n"
        "`Serie A`"
    )

    bot.reply_to(
        message,
        testo,
        parse_mode="Markdown"
    )


# ============================================================
# COMANDO /CAMPIONATI
# ============================================================

@bot.message_handler(
    commands=["campionati"]
)
def lista_campionati(message):

    testo = (
        "🏆 *CAMPIONATI DISPONIBILI*\n\n"
        "🇮🇹 Serie A\n"
        "🏴 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1\n\n"
        "Scrivi semplicemente il campionato."
    )

    bot.reply_to(
        message,
        testo,
        parse_mode="Markdown"
    )


# ============================================================
# GESTIONE CAMPIONATO
# ============================================================

@bot.message_handler(
    func=lambda message:
    message.text is not None
)
def genera_pronostici(message):

    campionato_utente = (
        message.text
        .strip()
        .lower()
    )

    # --------------------------------------------------------
    # CONTROLLO CAMPIONATO
    # --------------------------------------------------------

    if campionato_utente not in CAMPIONATI:

        bot.reply_to(
            message,
            "⚠️ *Campionato non riconosciuto.*\n\n"
            "Puoi scrivere:\n"
            "🇮🇹 Serie A\n"
            "🏴 Premier League\n"
            "🇪🇸 La Liga\n"
            "🇩🇪 Bundesliga\n"
            "🇫🇷 Ligue 1",
            parse_mode="Markdown"
        )

        return

    campionato = CAMPIONATI[
        campionato_utente
    ]

    nome_campionato = campionato["nome"]

    league_id = campionato["league_id"]


    # --------------------------------------------------------
    # MESSAGGIO ELABORAZIONE
    # --------------------------------------------------------

    messaggio = bot.reply_to(
        message,
        "🔄 *CONNESSIONE API-FOOTBALL...*\n\n"
        f"{nome_campionato}\n\n"
        "📅 Ricerca partite reali...\n"
        "📊 Recupero dati...\n"
        "🤖 Preparazione pronostici...",
        parse_mode="Markdown"
    )


    try:

        # ----------------------------------------------------
        # RECUPERO PARTITE
        # ----------------------------------------------------

        partite = recupera_partite(
            league_id
        )

        if not partite:

            bot.edit_message_text(
                "ℹ️ *Nessuna partita trovata.*\n\n"
                f"Non risultano partite future di "
                f"{nome_campionato} nei prossimi 14 giorni.",
                message.chat.id,
                messaggio.message_id,
                parse_mode="Markdown"
            )

            return


        # ----------------------------------------------------
        # REPORT
        # ----------------------------------------------------

        report = (
            "🔮 *SCHEDINA AUTOMATICA AI*\n\n"
            f"{nome_campionato}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )


        # Limite prudenziale per evitare messaggi enormi
        partite_da_mostrare = partite[:10]


        for partita in partite_da_mostrare:

            fixture = partita.get(
                "fixture",
                {}
            )

            teams = partita.get(
                "teams",
                {}
            )

            fixture_id = fixture.get(
                "id"
            )

            data_partita = fixture.get(
                "date",
                ""
            )

            casa = teams.get(
                "home",
                {}
            ).get(
                "name",
                "Casa"
            )

            ospite = teams.get(
                "away",
                {}
            ).get(
                "name",
                "Ospite"
            )


            # ------------------------------------------------
            # PRONOSTICO
            # ------------------------------------------------

            prediction = recupera_pronostico(
                fixture_id
            )

            analisi = analizza_pronostico(
                prediction
            )


            # ------------------------------------------------
            # DATA
            # ------------------------------------------------

            data_formattata = formatta_data(
                data_partita
            )


            # ------------------------------------------------
            # PERCENTUALI
            # ------------------------------------------------

            prob_home = percentuale(
                analisi["home"]
            )

            prob_draw = percentuale(
                analisi["draw"]
            )

            prob_away = percentuale(
                analisi["away"]
            )


            # ------------------------------------------------
            # REPORT PARTITA
            # ------------------------------------------------

            report += (
                f"⚽ *{casa} - {ospite}*\n"
                f"📅 {data_formattata}\n\n"
                f"📊 *Probabilità 1X2*\n"
                f"1️⃣ {prob_home}\n"
                f"❌ X {prob_draw}\n"
                f"2️⃣ {prob_away}\n\n"
                f"🏆 *Favorita:* "
                f"{analisi['winner']}\n"
                f"💡 *Consiglio:* "
                f"{analisi['advice']}\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
            )


        # ----------------------------------------------------
        # AGGIUNTA INFO
        # ----------------------------------------------------

        if len(partite) > 10:

            report += (
                f"ℹ️ Ci sono altre "
                f"{len(partite) - 10} partite "
                "nel periodo selezionato."
            )


        # ----------------------------------------------------
        # INVIO RISULTATO
        # ----------------------------------------------------

        bot.edit_message_text(
            report,
            message.chat.id,
            messaggio.message_id,
            parse_mode="Markdown"
        )


    except Exception as e:

        print(
            f"ERRORE PRONOSTICI: {e}"
        )

        try:

            bot.edit_message_text(
                "❌ *Errore durante il recupero dei dati.*\n\n"
                "Controlla che la API Key sia corretta "
                "e che il servizio API-Football sia disponibile.",
                message.chat.id,
                messaggio.message_id,
                parse_mode="Markdown"
            )

        except Exception:

            bot.send_message(
                message.chat.id,
                "❌ Errore durante il recupero dei dati."
            )


# ============================================================
# SERVER HTTP PER RENDER
# ============================================================

class HealthCheckHandler(
    http.server.BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Telegram Calcio Bot - ONLINE"
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def run_server():

    server_address = (
        "0.0.0.0",
        PORT
    )

    httpd = http.server.HTTPServer(
        server_address,
        HealthCheckHandler
    )

    print(
        f"🌐 Server Render attivo sulla porta {PORT}"
    )

    httpd.serve_forever()


# ============================================================
# AVVIO PROGRAMMA
# ============================================================

if __name__ == "__main__":

    print(
        "🚀 Avvio Telegram Calcio Bot..."
    )

    print(
        f"🌐 Porta Render: {PORT}"
    )

    # Server HTTP per Render
    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()


    # Rimozione eventuale webhook
    try:

        bot.remove_webhook()

        print(
            "✅ Webhook rimosso."
        )

    except Exception as e:

        print(
            f"⚠️ Impossibile rimuovere webhook: {e}"
        )


    # Avvio Telegram
    print(
        "🤖 Telegram Bot ONLINE!"
    )

    bot.infinity_polling(
        skip_pending=True,
        timeout=60,
        long_polling_timeout=60
    )
