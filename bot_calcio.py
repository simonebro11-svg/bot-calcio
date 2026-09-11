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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_URL = (
    "https://bot-pronostici-gratis.onrender.com/telegram/webhook"
)

API_BASE_URL = "https://v3.football.api-sports.io"

# Stagione attuale: 2026 = stagione 2026/2027
CURRENT_SEASON = 2026

# Numero massimo di partite analizzate per richiesta
MAX_MATCHES = 10

# Cache per evitare chiamate inutili all'API
CACHE_TTL = 600  # 10 minuti


# ============================================================
# CAMPIONATI
# ============================================================

LEAGUES = {
    "Serie A": {
        "id": 135,
        "country": "Italy",
        "emoji": "🇮🇹",
    },
    "Premier League": {
        "id": 39,
        "country": "England",
        "emoji": "🏴",
    },
    "La Liga": {
        "id": 140,
        "country": "Spain",
        "emoji": "🇪🇸",
    },
    "Bundesliga": {
        "id": 78,
        "country": "Germany",
        "emoji": "🇩🇪",
    },
    "Ligue 1": {
        "id": 61,
        "country": "France",
        "emoji": "🇫🇷",
    },
}


# ============================================================
# CONTROLLO VARIABILI
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "ERRORE: variabile TELEGRAM_BOT_TOKEN non configurata."
    )

if not FOOTBALL_API_KEY:
    raise RuntimeError(
        "ERRORE: variabile FOOTBALL_API_KEY non configurata."
    )


print("==========================================")
print("⚽ BOT PRONOSTICI CALCIO")
print("Avvio applicazione Render...")
print("==========================================")
print("Server Render attivo.")
print("==========================================")
print("CONFIGURAZIONE TELEGRAM")
print("TELEGRAM_BOT_TOKEN: OK")
print("FOOTBALL_API_KEY: OK")
print("==========================================")


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# CACHE
# ============================================================

_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)

        if not item:
            return None

        timestamp, value = item

        if time.time() - timestamp > CACHE_TTL:
            del _cache[key]
            return None

        return value


def cache_set(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)


# ============================================================
# API-FOOTBALL
# ============================================================

def api_get(endpoint, params=None, cache_key=None, timeout=25):
    """
    Esegue una richiesta GET ad API-Football.
    """

    if cache_key:
        cached = cache_get(cache_key)

        if cached is not None:
            print(f"💾 CACHE: {cache_key}")
            return cached

    url = API_BASE_URL + endpoint

    headers = {
        "x-apisports-key": FOOTBALL_API_KEY,
        "Accept": "application/json",
    }

    try:
        print(f"🌐 API-FOOTBALL: {endpoint}")
        print(f"📋 PARAMETRI: {params}")

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=timeout
        )

        print(
            f"📡 HTTP {response.status_code}: "
            f"{response.url}"
        )

        if response.status_code != 200:
            print(
                f"❌ API-FOOTBALL HTTP {response.status_code}"
            )
            print(response.text[:1000])

            return None

        data = response.json()

        errors = data.get("errors")

        if errors:
            print(f"❌ API-FOOTBALL ERRORI: {errors}")
            return None

        if cache_key:
            cache_set(cache_key, data)

        return data

    except requests.exceptions.Timeout:
        print("❌ API-FOOTBALL: timeout")
        return None

    except requests.exceptions.RequestException as exc:
        print(f"❌ API-FOOTBALL REQUEST ERROR: {exc}")
        return None

    except Exception as exc:
        print(f"❌ API-FOOTBALL ERRORE GENERICO: {exc}")
        return None


# ============================================================
# TEST API
# ============================================================

def test_api():
    print("🔎 TEST API-FOOTBALL...")

    data = api_get(
        "/countries",
        cache_key="test_countries",
        timeout=20
    )

    if data is None:
        print("❌ TEST API FALLITO.")
        return False

    print("✅ API-FOOTBALL RAGGIUNGIBILE.")
    return True


# ============================================================
# RECUPERO PARTITE
# ============================================================

def get_upcoming_fixtures(league_name):
    """
    Recupera le prossime partite del campionato
    nei prossimi 14 giorni.
    """

    league = LEAGUES.get(league_name)

    if not league:
        return []

    league_id = league["id"]

    today = datetime.utcnow().date()
    end_date = today + timedelta(days=14)

    date_from = today.strftime("%Y-%m-%d")
    date_to = end_date.strftime("%Y-%m-%d")

    print("==================================================")
    print(f"📅 Recupero partite future: {league_name}")
    print(f"📆 Dal {date_from} al {date_to}")
    print("==================================================")

    cache_key = (
        f"fixtures_{league_id}_{CURRENT_SEASON}_"
        f"{date_from}_{date_to}"
    )

    data = api_get(
        "/fixtures",
        params={
            "league": league_id,
            "season": CURRENT_SEASON,
            "from": date_from,
            "to": date_to,
            "timezone": "Europe/Rome",
        },
        cache_key=cache_key
    )

    if not data:
        print("❌ Nessun dato ricevuto da API-FOOTBALL.")
        return []

    fixtures = data.get("response", [])

    print(
        f"✅ Partite ricevute da API-FOOTBALL: "
        f"{len(fixtures)}"
    )

    # Solo partite non ancora iniziate
    future_statuses = {
        "NS",
        "TBD",
    }

    future = []

    for fixture in fixtures:

        status = (
            fixture
            .get("fixture", {})
            .get("status", {})
            .get("short")
        )

        if status in future_statuses:
            future.append(fixture)

    # Ordina per data
    future.sort(
        key=lambda x: x.get("fixture", {}).get("timestamp", 0)
    )

    # Limite per non consumare troppe chiamate API
    future = future[:MAX_MATCHES]

    print(
        f"📊 Partite future selezionate: "
        f"{len(future)}"
    )

    return future


# ============================================================
# CLASSIFICA
# ============================================================

def get_standings(league_name):
    league = LEAGUES.get(league_name)

    if not league:
        return {}

    league_id = league["id"]

    cache_key = (
        f"standings_{league_id}_{CURRENT_SEASON}"
    )

    data = api_get(
        "/standings",
        params={
            "league": league_id,
            "season": CURRENT_SEASON,
        },
        cache_key=cache_key
    )

    if not data:
        return {}

    try:
        standings_groups = (
            data["response"][0]
            ["league"]
            ["standings"]
        )

        if not standings_groups:
            return {}

        table = standings_groups[0]

        result = {}

        for row in table:

            team = row.get("team", {})
            team_id = team.get("id")

            if team_id is None:
                continue

            result[team_id] = {
                "position": row.get("rank", "-"),
                "points": row.get("points", 0),
                "form": row.get("form") or "-",
                "played": row.get("all", {}).get("played", 0),
                "wins": row.get("all", {}).get("win", 0),
                "draws": row.get("all", {}).get("draw", 0),
                "losses": row.get("all", {}).get("lose", 0),
                "goals_for": row.get("all", {}).get("goals", {}).get(
                    "for", 0
                ),
                "goals_against": row.get("all", {}).get("goals", {}).get(
                    "against", 0
                ),
            }

        print(
            f"📊 Classifica caricata: "
            f"{len(result)} squadre"
        )

        return result

    except Exception as exc:
        print(f"⚠️ Errore lettura classifica: {exc}")
        return {}


# ============================================================
# PRONOSTICO API-FOOTBALL
# ============================================================

def get_prediction(fixture_id):

    cache_key = f"prediction_{fixture_id}"

    data = api_get(
        "/predictions",
        params={
            "fixture": fixture_id
        },
        cache_key=cache_key
    )

    if not data:
        return None

    response = data.get("response", [])

    if not response:
        return None

    return response[0]


# ============================================================
# FORMATTAZIONE PERCENTUALI
# ============================================================

def clean_percent(value):
    if value is None:
        return None

    try:
        return float(
            str(value).replace("%", "").replace(",", ".")
        )
    except Exception:
        return None


def percentage_text(value):
    number = clean_percent(value)

    if number is None:
        return "N/D"

    return f"{number:.0f}%"


# ============================================================
# CREAZIONE ANALISI PARTITA
# ============================================================

def analyze_fixture(fixture, standings):

    fixture_data = fixture.get("fixture", {})
    teams = fixture.get("teams", {})

    fixture_id = fixture_data.get("id")

    home = teams.get("home", {})
    away = teams.get("away", {})

    home_id = home.get("id")
    away_id = away.get("id")

    home_name = home.get("name", "Casa")
    away_name = away.get("name", "Trasferta")

    timestamp = fixture_data.get("timestamp")

    if timestamp:
        match_date = datetime.fromtimestamp(timestamp)
        date_text = match_date.strftime("%d/%m/%Y %H:%M")
    else:
        date_text = "Orario N/D"

    home_table = standings.get(home_id, {})
    away_table = standings.get(away_id, {})

    prediction = get_prediction(fixture_id)

    result = {
        "fixture_id": fixture_id,
        "home": home_name,
        "away": away_name,
        "date": date_text,
        "home_table": home_table,
        "away_table": away_table,
        "prediction": prediction,
    }

    return result


# ============================================================
# COSTRUZIONE PRONOSTICO
# ============================================================

def build_prediction_text(match):

    home = match["home"]
    away = match["away"]

    prediction = match.get("prediction")

    home_table = match.get("home_table", {})
    away_table = match.get("away_table", {})

    lines = []

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⚽ {home} - {away}")
    lines.append(f"🗓 {match['date']}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # Classifica
    home_pos = home_table.get("position", "-")
    away_pos = away_table.get("position", "-")

    home_points = home_table.get("points", 0)
    away_points = away_table.get("points", 0)

    lines.append(
        f"📊 Classifica: {home_pos}° ({home_points} pt) "
        f"vs {away_pos}° ({away_points} pt)"
    )

    # Forma
    home_form = home_table.get("form", "-")
    away_form = away_table.get("form", "-")

    lines.append(
        f"📈 Forma: {home_form} | {away_form}"
    )

    if not prediction:
        lines.append("")
        lines.append(
            "⚠️ Pronostico API non disponibile."
        )
        return "\n".join(lines)

    predictions = prediction.get("predictions", {})

    winner = predictions.get("winner", {})
    advice = predictions.get("advice")
    under_over = predictions.get("under_over")

    percent = predictions.get("percent", {})

    home_percent = percent.get("home")
    draw_percent = percent.get("draw")
    away_percent = percent.get("away")

    # Risultato consigliato
    winner_name = winner.get("name")

    if winner_name:
        if winner_name == home:
            pronostico = "1"
        elif winner_name == away:
            pronostico = "2"
        else:
            pronostico = winner_name
    else:
        pronostico = "N/D"

    lines.append("")
    lines.append("🔮 PRONOSTICO API-FOOTBALL")
    lines.append(
        f"🎯 Esito principale: {pronostico}"
    )

    lines.append(
        f"🏠 {home}: {percentage_text(home_percent)}"
    )

    lines.append(
        f"🤝 Pareggio: {percentage_text(draw_percent)}"
    )

    lines.append(
        f"🚗 {away}: {percentage_text(away_percent)}"
    )

    if under_over:
        lines.append(
            f"⚽ Goal: {under_over}"
        )

    if advice:
        lines.append(
            f"💡 Consiglio: {advice}"
        )

    # Punteggio previsto
    goals = predictions.get("goals", {})

    home_goals = goals.get("home")
    away_goals = goals.get("away")

    if home_goals is not None or away_goals is not None:
        lines.append(
            f"🥅 Risultato previsto: "
            f"{home_goals if home_goals is not None else '?'}-"
            f"{away_goals if away_goals is not None else '?'}"
        )

    return "\n".join(lines)


# ============================================================
# REPORT COMPLETO
# ============================================================

def generate_report(league_name):

    print(f"📨 RICHIESTA CAMPIONATO: {league_name}")

    fixtures = get_upcoming_fixtures(league_name)

    if not fixtures:

        return (
            f"⚽ {league_name}\n\n"
            "❌ Non sono riuscito a recuperare "
            "le prossime partite.\n\n"
            "Controlla i log di Render per eventuali "
            "errori API-Football."
        )

    standings = get_standings(league_name)

    print(
        f"🏁 ANALISI IN CORSO: "
        f"{len(fixtures)} partite"
    )

    reports = []

    for index, fixture in enumerate(fixtures, start=1):

        print(
            f"🔎 Analisi {index}/{len(fixtures)}..."
        )

        try:
            match = analyze_fixture(
                fixture,
                standings
            )

            reports.append(
                build_prediction_text(match)
            )

        except Exception as exc:

            print(
                f"⚠️ Errore analisi partita: {exc}"
            )

    header = (
        f"⚽ PRONOSTICI {league_name.upper()}\n"
        f"📅 Prossime partite\n"
        f"🤖 Analisi API-FOOTBALL\n\n"
    )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Pronostici generati automaticamente.\n"
        "Le percentuali sono quelle fornite "
        "dall'algoritmo API-Football.\n"
        "Gioca responsabilmente."
    )

    report = header + "\n\n".join(reports) + footer

    print(
        f"📊 Report generato: "
        f"{len(report)} caratteri"
    )

    return report


# ============================================================
# INVIO TELEGRAM
# ============================================================

def send_long_message(chat_id, text):

    # Telegram permette messaggi fino a circa 4096 caratteri.
    # Manteniamo un margine di sicurezza.
    max_length = 3900

    chunks = []

    while len(text) > max_length:

        cut = text.rfind(
            "\n",
            0,
            max_length
        )

        if cut < 1000:
            cut = max_length

        chunks.append(
            text[:cut]
        )

        text = text[cut:].lstrip()

    if text:
        chunks.append(text)

    print(
        f"📏 Lunghezza totale: "
        f"{sum(len(x) for x in chunks)} caratteri"
    )

    print(
        f"📨 Messaggi da inviare: "
        f"{len(chunks)}"
    )

    for index, chunk in enumerate(chunks, start=1):

        print(
            f"📤 Invio messaggio "
            f"{index}/{len(chunks)} "
            f"({len(chunk)} caratteri)"
        )

        try:

            bot.send_message(
                chat_id,
                chunk
            )

            print(
                f"✅ Messaggio "
                f"{index}/{len(chunks)} inviato."
            )

        except Exception as exc:

            print(
                f"❌ Errore invio Telegram: "
                f"{exc}"
            )

        # Piccola pausa per evitare invii troppo ravvicinati
        if index < len(chunks):
            time.sleep(1)


# ============================================================
# MENU TELEGRAM
# ============================================================

def create_menu():

    keyboard = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    keyboard.row(
        "🇮🇹 Serie A",
        "🏴 Premier League"
    )

    keyboard.row(
        "🇪🇸 La Liga",
        "🇩🇪 Bundesliga"
    )

    keyboard.row(
        "🇫🇷 Ligue 1"
    )

    return keyboard


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):

    print(
        f"👤 /start ricevuto da "
        f"{message.from_user.id}"
    )

    testo = (
        "⚽ BENVENUTO NEL BOT PRONOSTICI CALCIO!\n\n"
        "Scegli il campionato che vuoi analizzare.\n\n"
        "📊 Il bot recupererà le prossime partite "
        "e genererà i pronostici tramite API-Football."
    )

    bot.send_message(
        message.chat.id,
        testo,
        reply_markup=create_menu()
    )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_command(message):

    testo = (
        "ℹ️ COME USARE IL BOT\n\n"
        "Premi uno dei pulsanti del menu per "
        "analizzare un campionato.\n\n"
        "Campionati disponibili:\n"
        "🇮🇹 Serie A\n"
        "🏴 Premier League\n"
        "🇪🇸 La Liga\n"
        "🇩🇪 Bundesliga\n"
        "🇫🇷 Ligue 1"
    )

    bot.send_message(
        message.chat.id,
        testo,
        reply_markup=create_menu()
    )


# ============================================================
# GESTIONE CAMPIONATI
# ============================================================

@bot.message_handler(
    func=lambda message:
        message.text in [
            "🇮🇹 Serie A",
            "🏴 Premier League",
            "🇪🇸 La Liga",
            "🇩🇪 Bundesliga",
            "🇫🇷 Ligue 1",
        ]
)
def championship_handler(message):

    mapping = {
        "🇮🇹 Serie A": "Serie A",
        "🏴 Premier League": "Premier League",
        "🇪🇸 La Liga": "La Liga",
        "🇩🇪 Bundesliga": "Bundesliga",
        "🇫🇷 Ligue 1": "Ligue 1",
    }

    league_name = mapping.get(message.text)

    if not league_name:
        return

    print(
        f"📩 RICHIESTA CAMPIONATO: "
        f"{league_name}"
    )

    # Messaggio temporaneo
    waiting_message = bot.send_message(
        message.chat.id,
        (
            f"⏳ Sto analizzando "
            f"{league_name}...\n\n"
            "📅 Recupero le prossime partite\n"
            "📊 Analizzo classifica e forma\n"
            "🔮 Calcolo i pronostici\n\n"
            "Attendi qualche secondo..."
        )
    )

    def process():

        try:

            report = generate_report(
                league_name
            )

            print(
                "📤 Avvio invio Telegram..."
            )

            print(
                "🏁 REPORT TELEGRAM INVIATO "
                "COMPLETAMENTE."
            )

            send_long_message(
                message.chat.id,
                report
            )

            print(
                f"✅ ELABORAZIONE "
                f"{league_name} TERMINATA."
            )

        except Exception as exc:

            print(
                f"❌ ERRORE ELABORAZIONE "
                f"{league_name}: {exc}"
            )

            try:

                bot.send_message(
                    message.chat.id,
                    (
                        "❌ Si è verificato un errore "
                        "durante l'elaborazione.\n\n"
                        "Controlla i log di Render."
                    )
                )

            except Exception:
                pass

    # L'elaborazione avviene in background
    # così il webhook risponde rapidamente.
    threading.Thread(
        target=process,
        daemon=True
    ).start()


# ============================================================
# GESTIONE TESTO NON RICONOSCIUTO
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def unknown_message(message):

    bot.send_message(
        message.chat.id,
        (
            "⚽ Scegli un campionato dal menu "
            "qui sotto."
        ),
        reply_markup=create_menu()
    )


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():

    print("==========================================")
    print("📡 CONFIGURAZIONE WEBHOOK TELEGRAM")
    print("==========================================")

    try:

        # Rimuove eventuale vecchio webhook
        print("🧹 Rimozione eventuale webhook precedente...")

        bot.remove_webhook(
            timeout=30
        )

        time.sleep(1)

        # Imposta il nuovo webhook
        print(
            f"🔗 Impostazione webhook:\n"
            f"{WEBHOOK_URL}"
        )

        result = bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            timeout=30
        )

        print(
            f"✅ Webhook configurato: {result}"
        )

        print("==========================================")

        return True

    except Exception as exc:

        print(
            f"❌ ERRORE CONFIGURAZIONE WEBHOOK: "
            f"{exc}"
        )

        return False


# ============================================================
# HTTP SERVER RENDER
# ============================================================

class HealthHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Evita log HTTP inutilmente rumorosi
        return

    def do_GET(self):

        path = urlparse(
            self.path
        ).path

        if path == "/":

            response = {
                "status": "online",
                "bot": "Bot Pronostici Calcio",
                "data_source": "API-Football",
                "webhook": "active",
            }

            body = json.dumps(
                response,
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

            self.wfile.write(body)

            return

        if path == "/health":

            body = b"OK"

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        path = urlparse(
            self.path
        ).path

        if path != "/telegram/webhook":

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
                f"📩 POST RICEVUTA: {path}"
            )

            print(
                f"📩 Dimensione richiesta: "
                f"{content_length} byte"
            )

            body = self.rfile.read(
                content_length
            )

            update_json = json.loads(
                body.decode("utf-8")
            )

            print(
                "⚙️ Elaborazione update Telegram..."
            )

            update = (
                telebot.types.Update.de_json(
                    update_json
                )
            )

            # Telegram deve ricevere una risposta
            # rapidamente.
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
            def process_update():

                try:

                    bot.process_new_updates(
                        [update]
                    )

                    print(
                        "✅ Update Telegram elaborato."
                    )

                except Exception as exc:

                    print(
                        f"❌ Errore elaborazione "
                        f"update: {exc}"
                    )

            threading.Thread(
                target=process_update,
                daemon=True
            ).start()

        except Exception as exc:

            print(
                f"❌ ERRORE WEBHOOK: {exc}"
            )

            try:

                self.send_response(500)
                self.end_headers()

            except Exception:
                pass


# ============================================================
# AVVIO SERVER
# ============================================================

class ThreadingHTTPServer(
    socketserver.ThreadingMixIn,
    http.server.HTTPServer
):

    daemon_threads = True


def start_http_server():

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(
        f"🌐 Server HTTP avviato sulla porta "
        f"{PORT}"
    )

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

    print("==========================================")
    print("🚀 AVVIO BOT")
    print("==========================================")

    # Avvia HTTP server
    server = start_http_server()

    # Test API
    test_api()

    # Configura webhook
    webhook_ok = configura_webhook()

    if webhook_ok:

        print(
            "✅ BOT TELEGRAM ONLINE"
        )

        print(
            f"🔗 WEBHOOK: {WEBHOOK_URL}"
        )

    else:

        print(
            "⚠️ WEBHOOK NON CONFIGURATO."
        )

    print("==========================================")
    print("🟢 SERVER RENDER ATTIVO")
    print("==========================================")

    # Mantiene vivo il processo Render
    while True:

        time.sleep(60)

        print(
            "💓 Bot attivo - webhook operativo."
        )


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "🛑 Arresto manuale."
        )

    except Exception as exc:

        print(
            f"❌ ERRORE FATALE: {exc}"
        )

        raise
