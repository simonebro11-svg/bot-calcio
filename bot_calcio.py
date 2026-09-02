import telebot

# INSERISCI QUI IL TUO TOKEN DI TELEGRAM RILASCIATO DA BOTFATHER
TELEGRAM_BOT_TOKEN = "8977059725:AAFVBTr1uqaEeCXmcnRsjsGxktzmaD-Zdu8"
# SCEGLI UNA PASSWORD PER AGGIORNARE LE PARTITE DA TELEGRAM
PASSWORD_ADMIN = "mia2025"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Questo database ora vive nella memoria del bot e può essere modificato live via chat
DATABASE_CAMPIONATI = {
    "serie a": [
        {"casa": "Genoa", "ospite": "Como", "prob_1": 45, "prob_X": 32, "prob_2": 23, "consiglio": "🎯 1X / MULTIGOL 1-4"},
        {"casa": "Fiorentina", "ospite": "Torino", "prob_1": 42, "prob_X": 33, "prob_2": 25, "consiglio": "🎯 UNDER 2.5"}
    ],
    "premier league": [],
    "la liga": [],
    "bundesliga": [],
    "ligue 1": []
}

@bot.message_handler(commands=['start', 'help'])
def invia_benvenuto(message):
    guida = "🤖 **Generatore AI Palinsesti Europei Attivo!** ⚽\n\n"
    guida += "Scrivimi il nome del campionato per ricevere la schedina completa delle partite del turno:\n"
    guida += "👉 `Serie A`, `Premier League`, `La Liga`, `Bundesliga`, `Ligue 1`"
    bot.reply_to(message, guida, parse_mode="Markdown")

# COMANDO SEGRETO PER CAMBIARE LE SQUADRE DA TELEGRAM
@bot.message_handler(commands=['update'])
def aggiorna_partite(message):
    istruzioni = "⚙️ **PANNELLO DI AGGIORNAMENTO DIRETTO** ⚙️\n\n"
    istruzioni += "Per caricare il nuovo turno, invia un messaggio formattato esattamente così:\n\n"
    istruzioni += "`admin123 | serie a | Casa vs Ospite (50%-30%-20%) - Consiglio`\n\n"
    istruzioni += "💡 *Puoi inserire più partite andando a capo per ognuna dopo il simbolo |*"
    bot.reply_to(message, istruzioni, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def gestisci_messaggi(message):
    testo = message.text.strip()
    
    # Rilevamento tentativo di aggiornamento palinsesto
    if "|" in testo:
        try:
            parti = testo.split("|")
            password = parti[0].strip()
            campionato = parti[1].strip().lower()
            
            if password != PASSWORD_ADMIN:
                bot.reply_to(message, "❌ Password amministratore errata!")
                return
                
            if campeonato not in DATABASE_CAMPIONATI:
                bot.reply_to(message, "❌ Campionato non esistente. Usa: `serie a`, `premier league`, `la liga`, `bundesliga` o `ligue 1`.")
                return
            
            nuove_partite = []
            # Elabora ogni riga di partita inviata
            for riga in parti[2:]:
                if "vs" in riga:
                    info, consiglio = riga.split("-")
                    squadre, percentuali = info.split("(")
                    casa, ospite = squadre.split("vs")
                    p1, px, p2 = percentuali.replace(")", "").replace("%", "").split("-")
                    
                    nuove_partite.append({
                        "casa": casa.strip(),
                        "ospite": ospite.strip(),
                        "prob_1": int(p1),
                        "prob_X": int(px),
                        "prob_2": int(p2),
                        "consiglio": "🎯 " + consiglio.strip()
                    })
            
            if nuove_partite:
                DATABASE_CAMPIONATI[campionato] = nuove_partite
                bot.reply_to(message, f"✅ Palinsesto **{campionato.upper()}** aggiornato con successo con {len(nuove_partite)} partite!")
            else:
                bot.reply_to(message, "❌ Errore nella lettura delle righe delle squadre.")
        except Exception as e:
            bot.reply_to(message, f"❌ Errore di formattazione. Rilevato: {e}")
        return

    # Visualizzazione classica dei pronostici
    campionato_utente = testo.lower()
    if campionato_utente in DATABASE_CAMPIONATI:
        partite = DATABASE_CAMPIONATI[campionato_utente]
        if not int(len(partite)):
            bot.reply_to(message, "📅 Palinsesto vuoto per questo campionato. Usa /update per caricarlo.")
            return
            
        report = f"🔮 **SCHEDINA INTERA: {testo.upper()}** 🔮\n\n"
        for match in partite:
            report += f"⚔️ **{match['casa']} vs {match['ospite']}**\n"
            report += f"📊 Algoritmo 1X2: 1 ({match['prob_1']}%) | X ({match['prob_X']}%) | 2 ({match['prob_2']}%)\n"
            report += f"{match['consiglio']}\n"
            report += "----------------------------------------\n"
        bot.send_message(message.chat.id, report, parse_mode="Markdown")

print("🚀 Bot Admin-Live pronto e attivo sul tuo Desktop!")
bot.infinity_polling()
