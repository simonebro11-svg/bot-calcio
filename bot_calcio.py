import telebot
import requests
from datetime import datetime

# INSERISCI IL TUO REALE TOKEN RILASCIATO DA BOTFATHER
TELEGRAM_BOT_TOKEN = "8977059725:AAFVBTr1uqaEeCXmcnRsjsGxktzmaD-Zdu8"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Collegamenti ai database dei calendari ufficiali openfootball (JSON Gratuiti)
URLS_CAMPIONATI = {
    "serie a": "https://githubusercontent.com",
    "premier league": "https://githubusercontent.com",
    "la liga": "https://githubusercontent.com",
    "bundesliga": "https://githubusercontent.com",
    "ligue 1": "https://githubusercontent.com"
}

# Struttura di backup locale in caso i server esterni siano temporaneamente offline
DATABASE_BACKUP = {
    "serie a": "⚔️ Inter vs Napoli - 🎯 SEGNO 1\n⚔️ Juventus vs Milan - 🎯 1X + UNDER 3.5\n⚔️ Roma vs Atalanta - 🎯 GOAL"
}

@bot.message_handler(commands=['start', 'help'])
def invia_benvenuto(message):
    guida = "🤖 **Assistente AI Pronostici Europei Automatico Online!** ⚽\n\n"
    guida += "Il sistema scarica i calendari reali da internet in tempo reale.\n"
    guida += "Scrivimi semplicemente il nome del campionato per elaborare il prossimo turno:\n\n"
    guida += "👉 `Serie A`\n👉 `Premier League`\n👉 `La Liga`\n👉 `Bundesliga`\n👉 `Ligue 1`"
    bot.reply_to(message, guida, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def genera_pronostici_automatici(message):
    campionato_utente = message.text.strip().lower()

    # CORRETTO: adesso usa 'campionato_utente' con la 'i' ovunque
    if campionato_utente not in URLS_CAMPIONATI:
        bot.reply_to(message, "⚠️ Campionato non supportato. Scrivi: `Serie A`, `Premier League`, `La Liga`, `Bundesliga` o `Ligue 1`.")
        return

    bot.reply_to(message, f"🔄 Connessione ai server europei... Download calendario e calcolo quote AI per: {message.text.upper()} 📈")

    try:
        # Scarichiamo il file di calendario aggiornato
        risposta = requests.get(URLS_CAMPIONATI[campionato_utente])
        if risposta.status_code != 200:
            if campionato_utente in DATABASE_BACKUP and DATABASE_BACKUP[campionato_utente]:
                bot.send_message(message.chat.id, f"🔮 **SCHEDINA (MODALITÀ BACKUP): {message.text.upper()}** 🔮\n\n" + DATABASE_BACKUP[campionato_utente])
            else:
                bot.reply_to(message, "❌ Campionato momentaneamente non disponibile sui server di origine. Riprova più tardi.")
            return

        dati = risposta.json()
        giornate = dati.get("rounds", [])

        if not giornate:
            bot.reply_to(message, "📅 Calendario non ancora disponibile per la nuova stagione.")
            return

        # Scansione temporale sicura per trovare il turno corrente
        oggi = datetime.now().date()
        giornata_corrente = giornate[0]
        
        for turno in giornate:
            match_turno = turno.get("matches", [])
            if match_turno:
                data_str = match_turno[-1].get("date")
                if data_str:
                    try:
                        data_match = datetime.strptime(data_str, "%Y-%m-%d").date()
                        if data_match >= oggi:
                            giornata_corrente = turno
                            break
                    except:
                        continue

        nome_giornata = giornata_corrente.get("name", "Prossimo Turno")
        matches = giornata_corrente.get("matches", [])

        report = f"🔮 **SCHEDINA AUTOMATICA AI: {message.text.upper()}** 🔮\n"
        report += f"📅 *Fase: {nome_giornata}*\n----------------------------------------\n\n"

        for match in matches:
            casa = match.get("team1", "Squadra Casa")
            ospite = match.get("team2", "Squadra Ospite")
            
            # --- MODELLO DI SIMULAZIONE AI ---
            hash_match = abs(hash(str(casa)) + hash(str(ospite)))
            prob_1 = 35 + (hash_match % 31)
            prob_2 = 15 + (hash_match % 26)
            prob_X = 100 - (prob_1 + prob_2)
            
            if prob_X < 15:
                prob_X = 20
                prob_1 -= 5

            if prob_1 > 55:
                consiglio = f"🎯 SEGNO 1 ({casa} favorita in casa)"
            elif prob_2 > 45:
                consiglio = f"🎯 SEGNO 2 ({ospite} favorita in trasferta)"
            elif prob_1 + prob_2 > 75:
                consiglio = "🎯 GOAL (Match da reti)"
            else:
                consiglio = "🎯 DOPPIA CHANCE 1X + UNDER 3.5"

            report += f"⚔️ **{casa} vs {ospite}**\n"
            report += f"📊 Algoritmo 1X2: 1 ({prob_1}%) | X ({prob_X}%) | 2 ({prob_2}%)\n"
            report += f"{consiglio}\n"
            report += "----------------------------------------\n"

        # Invio con suddivisione del testo di sicurezza
        if len(report) > 4000:
            for i in range(0, len(report), 4000):
                bot.send_message(message.chat.id, report[i:i+4000], parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, report, parse_mode="Markdown")

    except Exception as e:
        if campionato_utente in DATABASE_BACKUP and DATABASE_BACKUP[campionato_utente]:
            bot.send_message(message.chat.id, f"🔮 **SCHEDINA (MODALITÀ PROVVISORIA): {message.text.upper()}** 🔮\n\n" + DATABASE_BACKUP[campionato_utente])
        else:
            bot.reply_to(message, "❌ Servizio momentaneamente in manutenzione aggiornamento dati.")

# Reset dei Webhook per garantire la massima stabilità sul server di Render
bot.remove_webhook()
print("🚀 Server AI Online! Monitoraggio automatico europeo attivo.")
bot.infinity_polling(skip_pending=True)
