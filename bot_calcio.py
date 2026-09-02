import telebot

# INSERISCI QUI IL TUO TOKEN DI TELEGRAM RILASCIATO DA BOTFATHER
TELEGRAM_BOT_TOKEN = "8977059725:AAFVBTr1uqaEeCXmcnRsjsGxktzmaD-Zdu8"
PASSWORD_ADMIN = "mia2025"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Database dinamico memorizzato nel cloud
DATABASE_CAMPIONATI = {
    "serie a": "",
    "premier league": "",
    "la liga": "",
    "bundesliga": "",
    "ligue 1": ""
}

@bot.message_handler(commands=['start', 'help'])
def invia_benvenuto(message):
    guida = "🤖 **Generatore AI Palinsesti Europei Attivo!** ⚽\n\n"
    guida += "Scrivimi il nome del campionato per ricevere la schedina completa delle partite del turno:\n"
    guida += "👉 `Serie A`, `Premier League`, `La Liga`, `Bundesliga`, `Ligue 1`"
    bot.reply_to(message, guida, parse_mode="Markdown")

@bot.message_handler(commands=['update'])
def aggiorna_partite(message):
    istruzioni = "⚙️ **PANNELLO DI AGGIORNAMENTO DIRETTO** ⚙️\n\n"
    istruzioni += "Per caricare il nuovo turno, invia un messaggio scritto semplicemente così:\n\n"
    istruzioni += "`admin123`\n"
    istruzioni += "`serie a`\n"
    istruzioni += "ℹ️ *Incolla qui sotto l'elenco completo dei match con i tuoi pronostici, andando semplicemente a capo per ogni riga!*"
    bot.reply_to(message, istruzioni, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def gestisci_messaggi(message):
    testo = message.text.strip()
    linee = testo.split("\n")
    
    # Rilevamento pannello amministratore (se la prima riga è la password)
    if linee[0].lower() == PASSWORD_ADMIN.lower():
        if len(linee) < 3:
            bot.reply_to(message, "❌ Errore: Inserisci la password, vai a capo, scrivi il campionato e poi incolla il palinsesto.")
            return
            
        campionato = linee[1].strip().lower()
        if campionato not in DATABASE_CAMPIONATI:
            bot.reply_to(message, "❌ Campionato errato. Usa: `serie a`, `premier league`, `la liga`, `bundesliga` o `ligue 1`.")
            return
            
        # Uniamo tutte le linee successive per creare il testo del palinsesto
        corpo_palinsesto = "\n".join(linee[2:])
        DATABASE_CAMPIONATI[campionato] = corpo_palinsesto
        
        bot.reply_to(message, f"✅ Palinsesto **{campionato.upper()}** aggiornato con successo!")
        return

    # Visualizzazione classica dei pronostici per l'utente
    campionato_utente = testo.lower()
    if campionato_utente in DATABASE_CAMPIONATI:
        contenuto = DATABASE_CAMPIONATI[campionato_utente]
        if not contenuto:
            bot.reply_to(message, "📅 Il palinsesto per questo campionato è attualmente vuoto. Usa il comando /update per caricarlo.")
            return
            
        report = f"🔮 **SCHEDINA INTERA: {testo.upper()}** 🔮\n\n"
        report += contenuto
        
        # Divisione di sicurezza per i limiti di testo di Telegram
        if len(report) > 4000:
            for i in range(0, len(report), 4000):
                bot.send_message(message.chat.id, report[i:i+4000], parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, report, parse_mode="Markdown")

# Avvio del bot con rimozione dei webhook pendenti per evitare conflitti
bot.remove_webhook()
print("🚀 Bot in ascolto continuo nel Cloud!")
bot.infinity_polling(skip_pending=True)
