import telebot
import http.server
import threading

# INSERISCI QUI IL TUO REALE TOKEN RILASCIATO DA BOTFATHER
TELEGRAM_BOT_TOKEN = "8977059725:AAFVBTr1uqaEeCXmcnRsjsGxktzmaD-Zdu8"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# DATABASE EUROPEO REALE COMPLETO (STAGIONE ATTUALE)
SQUADRE_EUROPEE = {
    "serie a": [
        "Inter", "Napoli", "Juventus", "Milan", "Roma", "Atalanta", "Lazio", "Fiorentina", 
        "Torino", "Bologna", "Genoa", "Monza", "Verona", "Lecce", "Udinese", "Cagliari", 
        "Empoli", "Parma", "Como", "Venezia"
    ],
    "premier league": [
        "Manchester City", "Arsenal", "Liverpool", "Aston Villa", "Tottenham", "Chelsea", 
        "Newcastle", "Manchester United", "West Ham", "Brighton", "Bournemouth", "Crystal Palace", 
        "Fulham", "Everton", "Brentford", "Nottingham Forest", "Leicester", "Ipswich Town", 
        "Southampton", "Leeds United"
    ],
    "la liga": [
        "Real Madrid", "Barcellona", "Girona", "Atletico Madrid", "Athletic Bilbao", "Real Sociedad", 
        "Betis", "Valencia", "Alaves", "Osasuna", "Getafe", "Celta Vigo", "Sevilla", 
        "Mallorca", "Las Palmas", "Rayo Vallecano", "Leganes", "Valladolid", "Espanyol", "Malaga"
    ],
    "bundesliga": [
        "Bayer Leverkusen", "Stoccarda", "Bayern Monaco", "RB Lipsia", "Borussia Dortmund", 
        "Francoforte", "Hoffenheim", "Heidenheim", "Brema", "Friburgo", "Augusta", "Wolfsburg", 
        "Magonza", "Gladbach", "Union Berlino", "Bochum", "St. Pauli", "Holstein Kiel"
    ],
    "ligue 1": [
        "Paris Saint-Germain", "Monaco", "Brest", "Lille", "Nizza", "Lione", 
        "Lens", "Marsiglia", "Reims", "Rennes", "Tolosa", "Montpellier", 
        "Strasburgo", "Nantes", "Le Havre", "Auxerre", "Angers", "Saint-Étienne"
    ]
}

@bot.message_handler(commands=['start', 'help'])
def invia_benvenuto(message):
    guida = "🤖 **Generatore Automatico Pronostici AI Attivo!** ⚽\n\n"
    guida += "Il sistema genera l'intero palinsesto del turno a 10 partite.\n"
    guida += "Scrivimi semplicemente il nome del campionato per ricevere la schedina:\n\n"
    guida += "👉 `Serie A`\n👉 `Premier League`\n👉 `La Liga`\n👉 `Bundesliga`\n👉 `Ligue 1`"
    bot.reply_to(message, guida, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def genera_palinsesto_automatico(message):
    # BLINDATO: Variabile scritta con la 'i' ovunque per annullare i NameError dei log
    campionato_utente = message.text.strip().lower()

    if campionato_utente not in SQUADRE_EUROPEE:
        bot.reply_to(message, "⚠️ Campionato non supportato. Scrivi: `Serie A`, `Premier League`, `La Liga`, `Bundesliga` o `Ligue 1`.")
        return

    bot.reply_to(message, f"🔄 AI: Elaborazione simulazioni algoritmiche 1X2 per la giornata di {message.text.upper()}... 📈")

    try:
        lista_squadre = SQUADRE_EUROPEE[campionato_utente]
        
        matches = []
        metascoro = len(lista_squadre) // 2
        for i in range(metascoro):
            casa = lista_squadre[i]
            ospite = lista_squadre[len(lista_squadre) - 1 - i]
            matches.append((casa, ospite))

        report = f"🔮 **SCHEDINA AUTOMATICA AI: {message.text.upper()}** 🔮\n"
        report += f"📅 *Palinsesto Completo Generato H24*\n----------------------------------------\n\n"

        for casa, ospite in matches:
            hash_match = abs(hash(str(casa)) + hash(str(ospite)))
            prob_1 = 38 + (hash_match % 26)
            prob_2 = 18 + (hash_match % 21)
            prob_X = 100 - (prob_1 + prob_2)
            
            if prob_X < 15:
                prob_X = 22
                prob_1 -= 7

            if prob_1 > 54:
                consiglio = f"🎯 SEGNO 1 ({casa} favorita in casa)"
            elif prob_2 > 45:
                consiglio = f"🎯 SEGNO 2 ({ospite} del valore in trasferta)"
            elif prob_1 + prob_2 > 74:
                consiglio = "🎯 GOAL (Match aperto con reti)"
            else:
                consiglio = "🎯 DOPPIA CHANCE 1X + UNDER 3.5"

            report += f"⚔️ **{casa} vs {ospite}**\n"
            report += f"📊 Algoritmo 1X2: 1 ({prob_1}%) | X ({prob_X}%) | 2 ({prob_2}%)\n"
            report += f"{consiglio}\n"
            report += "----------------------------------------\n"

        bot.send_message(message.chat.id, report, parse_mode="Markdown")

    except Exception as e:
        bot.reply_to(message, f"❌ Errore durante la simulazione del palinsesto: {e}")

# --- MINI SERVER PER EVITARE IL BLOCCO PORTE DI RENDER ---
def run_fake_server():
    server_address = ('', 10000)
    httpd = http.server.HTTPServer(server_address, http.server.SimpleHTTPRequestHandler)
    print("🌍 Server civetta per Render attivo sulla porta 10000.")
    httpd.serve_forever()

# Avvio del server su un canale parallelo (Thread)
threading.Thread(target=run_fake_server, daemon=True).start()

# Connessione a Telegram pulita
bot.remove_webhook()
print("🚀 Server Autonomo Online! Schedine europee attive.")
bot.infinity_polling(skip_pending=True)
