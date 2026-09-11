import os
import json
import time
import threading
import http.server
from datetime import datetime

import requests
import telebot


# ============================================================
# CONFIGURAZIONE RENDER - SECRET FILE
# ============================================================

SECRET_FILE = "/etc/secrets/bot_secrets.env"


def carica_secret_file():

    if not os.path.exists(SECRET_FILE):

        print(
            "⚠️ Secret File non trovato.",
            flush=True
        )

        return

    try:

        with open(
            SECRET_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            for line in file:

                line = line.strip()

                if (
                    not line
                    or line.startswith("#")
                    or "=" not in line
                ):
                    continue

                key, value = line.split(
                    "=",
                    1
                )

                key = key.strip()
                value = value.strip()

                if not os.getenv(key):

                    os.environ[key] = value

        print(
            "✅ Secret File caricato correttamente.",
            flush=True
        )

    except Exception as e:

        print(
            f"❌ Errore lettura Secret File: {e}",
            flush=True
        )


carica_secret_file()


# ============================================================
# CONFIGURAZIONE
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

FOOTBALL_API_KEY = os.getenv(
    "FOOTBALL_API_KEY"
)

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://bot-pronostici-gratis.onrender.com"
).rstrip("/")


WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_URL = (
    RENDER_EXTERNAL_URL
    + WEBHOOK_PATH
)


API_BASE_URL = (
    "https://v3.football.api-sports.io"
)


# ============================================================
# CONTROLLO CREDENZIALI
# ============================================================

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError(
        "❌ TELEGRAM_BOT_TOKEN non configurato."
    )


if not FOOTBALL_API_KEY:

    raise RuntimeError(
        "❌ FOOTBALL_API_KEY non configurato."
    )


print(
    "TELEGRAM_BOT_TOKEN: OK",
    flush=True
)

print(
    "FOOTBALL_API_KEY: OK",
    flush=True
)


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# CAMPIONATI
# ============================================================

CAMPIONATI = {

    "🇮🇹 Serie A": {
        "id": 135,
        "nome": "Serie A"
    },

    "🇬🇧 Premier League": {
        "id": 39,
        "nome": "Premier League"
    },

    "🇪🇸 La Liga": {
        "id": 140,
        "nome": "La Liga"
    },

    "🇩🇪 Bundesliga": {
        "id": 78,
        "nome": "Bundesliga"
    },

    "🇫🇷 Ligue 1": {
        "id": 61,
        "nome": "Ligue 1"
    }

}


# ============================================================
# CONFIGURAZIONE ANALISI
# ============================================================

NUMERO_PARTITE_REPORT = 8

NUMERO_PARTITE_FORM = 10

API_TIMEOUT = 30


# ============================================================
# CACHE
# ============================================================

CACHE = {}

CACHE_LOCK = threading.Lock()

CACHE_TTL = 600


def cache_get(chiave):

    with CACHE_LOCK:

        elemento = CACHE.get(chiave)

        if not elemento:

            return None

        timestamp, valore = elemento

        if time.time() - timestamp > CACHE_TTL:

            del CACHE[chiave]

            return None

        return valore


def cache_set(
    chiave,
    valore
):

    with CACHE_LOCK:

        CACHE[chiave] = (
            time.time(),
            valore
        )


# ============================================================
# FUNZIONE API-FOOTBALL
# ============================================================

def api_get(
    endpoint,
    params=None
):

    chiave_cache = (
        endpoint,
        tuple(
            sorted(
                (params or {}).items()
            )
        )
    )


    dati_cache = cache_get(
        chiave_cache
    )


    if dati_cache is not None:

        print(
            f"♻️ CACHE API: /{endpoint}",
            flush=True
        )

        return dati_cache


    url = (
        f"{API_BASE_URL}/{endpoint}"
    )


    headers = {

        "x-apisports-key":
            FOOTBALL_API_KEY,

        "Accept":
            "application/json"

    }


    print(
        "==========================================",
        flush=True
    )

    print(
        f"🌐 API-FOOTBALL: /{endpoint}",
        flush=True
    )

    print(
        f"📋 PARAMETRI: {params}",
        flush=True
    )


    try:

        response = requests.get(

            url,

            headers=headers,

            params=params,

            timeout=API_TIMEOUT

        )


        print(
            f"📡 HTTP {response.status_code}: {url}",
            flush=True
        )


        if response.status_code != 200:

            print(
                f"❌ Risposta HTTP non valida: "
                f"{response.status_code}",
                flush=True
            )

            print(
                response.text[:1000],
                flush=True
            )

            return None


        dati = response.json()


        errori = dati.get(
            "errors"
        )


        if errori:

            print(
                f"❌ Errori API: {errori}",
                flush=True
            )

            return None


        risposta = dati.get(
            "response"
        )


        if risposta is None:

            print(
                "⚠️ API senza campo response.",
                flush=True
            )

            return None


        cache_set(
            chiave_cache,
            dati
        )


        return dati


    except requests.exceptions.Timeout:

        print(
            "❌ Timeout API-Football.",
            flush=True
        )

        return None


    except requests.exceptions.RequestException as e:

        print(
            f"❌ Errore richiesta API-Football: {e}",
            flush=True
        )

        return None


    except ValueError as e:

        print(
            f"❌ Errore JSON API-Football: {e}",
            flush=True
        )

        return None


# ============================================================
# TEST API
# ============================================================

def test_api():

    print(
        "==========================================",
        flush=True
    )

    print(
        "🔎 TEST API-FOOTBALL...",
        flush=True
    )


    dati = api_get(
        "countries"
    )


    if dati is not None:

        print(
            "✅ API-FOOTBALL RAGGIUNGIBILE.",
            flush=True
        )

        print(
            "==========================================",
            flush=True
        )

        return True


    print(
        "❌ API-FOOTBALL NON RAGGIUNGIBILE.",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    return False


# ============================================================
# FORMATTA DATA
# ============================================================

def formatta_data(
    data_string
):

    if not data_string:

        return "Data non disponibile"


    try:

        data = datetime.fromisoformat(

            data_string.replace(
                "Z",
                "+00:00"
            )

        )


        try:

            from zoneinfo import ZoneInfo

            if data.tzinfo:

                data = data.astimezone(
                    ZoneInfo("Europe/Rome")
                )

        except Exception:

            pass


        return data.strftime(
            "%d/%m/%Y %H:%M"
        )


    except Exception:

        return str(
            data_string
        )


# ============================================================
# RECUPERA PROSSIME PARTITE
#
# IMPORTANTE:
#
# Il piano Free dell'API-Football dell'utente
# restituisce:
#
# "Free plans do not have access to the
#  Next parameter."
#
# Inoltre blocca la stagione corrente 2026.
#
# NON facciamo quindi:
#
# next=8
# season=2026
# from=2026...
# to=2026...
#
# perché questi tentativi sono già stati rifiutati
# dalla chiave API.
#
# Viene effettuato UN SOLO tentativo diagnostico
# con la stagione corrente.
# ============================================================

def recupera_partite(
    league_id
):

    print(
        "==========================================",
        flush=True
    )

    print(
        "⚽ RICERCA PROSSIME PARTITE",
        flush=True
    )

    print(
        f"🏆 League ID: {league_id}",
        flush=True
    )

    print(
        "🔎 Controllo accesso calendario stagione corrente",
        flush=True
    )


    # --------------------------------------------------------
    # Stagione corrente.
    #
    # Dal mese di luglio in poi utilizziamo 2026.
    # --------------------------------------------------------

    anno = datetime.now().year

    mese = datetime.now().month


    if mese >= 7:

        stagione = anno

    else:

        stagione = anno - 1


    print(
        f"📅 Stagione rilevata: {stagione}",
        flush=True
    )


    # --------------------------------------------------------
    # TENTATIVO STANDARD
    #
    # Facciamo una sola richiesta.
    # Non usiamo next perché il piano Free lo blocca.
    # --------------------------------------------------------

    dati = api_get(

        "fixtures",

        {

            "league":
                league_id,

            "season":
                stagione,

            "timezone":
                "Europe/Rome"

        }

    )


    if not dati:

        print(
            "❌ API-Football non consente "
            "l'accesso al calendario richiesto.",
            flush=True
        )

        print(
            "⚠️ Il piano Free non permette "
            "di recuperare la stagione corrente.",
            flush=True
        )

        return []


    eventi = dati.get(
        "response",
        []
    )


    print(
        f"📊 Eventi ricevuti dall'API: "
        f"{len(eventi)}",
        flush=True
    )


    if not eventi:

        print(
            "⚠️ Nessun evento restituito.",
            flush=True
        )

        return []


    # --------------------------------------------------------
    # FILTRA SOLO PARTITE FUTURE
    # --------------------------------------------------------

    adesso = time.time()

    partite = []


    for evento in eventi:

        fixture = evento.get(
            "fixture",
            {}
        )

        teams = evento.get(
            "teams",
            {}
        )


        fixture_id = fixture.get(
            "id"
        )


        stato = (
            fixture
            .get(
                "status",
                {}
            )
            .get(
                "short",
                ""
            )
        )


        casa = (
            teams
            .get(
                "home",
                {}
            )
            .get(
                "name"
            )
        )


        trasferta = (
            teams
            .get(
                "away",
                {}
            )
            .get(
                "name"
            )
        )


        if not fixture_id:

            continue


        if not casa or not trasferta:

            continue


        stati_validi = (

            "NS",
            "TBD",
            "PST"

        )


        if stato not in stati_validi:

            continue


        data_partita = fixture.get(
            "date"
        )


        timestamp = fixture.get(
            "timestamp",
            0
        )


        if not timestamp:

            continue


        # Solo partite future
        if timestamp <= adesso:

            continue


        partite.append({

            "id":
                fixture_id,

            "casa":
                casa,

            "trasferta":
                trasferta,

            "data":
                data_partita,

            "timestamp":
                timestamp,

            "stato":
                stato,

            "casa_id":
                teams
                .get(
                    "home",
                    {}
                )
                .get(
                    "id"
                ),

            "trasferta_id":
                teams
                .get(
                    "away",
                    {}
                )
                .get(
                    "id"
                )

        })


    # --------------------------------------------------------
    # ORDINA
    # --------------------------------------------------------

    partite.sort(

        key=lambda x:
        x.get(
            "timestamp",
            0
        )

    )


    # --------------------------------------------------------
    # ELIMINA DUPLICATI
    # --------------------------------------------------------

    uniche = []

    ids_visti = set()


    for partita in partite:

        if partita["id"] in ids_visti:

            continue


        ids_visti.add(
            partita["id"]
        )


        uniche.append(
            partita
        )


    uniche = uniche[
        :NUMERO_PARTITE_REPORT
    ]


    print(
        f"✅ Prossime partite trovate: "
        f"{len(uniche)}",
        flush=True
    )


    for partita in uniche:

        print(

            f"⚽ {partita['casa']} - "
            f"{partita['trasferta']} | "
            f"{formatta_data(partita['data'])}",

            flush=True

        )


    print(
        "==========================================",
        flush=True
    )


    return uniche


# ============================================================
# RECUPERA FORMA RECENTE
# ============================================================

def recupera_form_squadra(
    team_id
):

    if not team_id:

        return []


    chiave = (
        "form",
        team_id
    )


    cached = cache_get(
        chiave
    )


    if cached is not None:

        return cached


    print(
        f"📈 Recupero ultime "
        f"{NUMERO_PARTITE_FORM} partite "
        f"della squadra {team_id}",
        flush=True
    )


    dati = api_get(

        "fixtures",

        {

            "team":
                team_id,

            "last":
                NUMERO_PARTITE_FORM

        }

    )


    if not dati:

        return []


    eventi = dati.get(
        "response",
        []
    )


    risultati = []


    for evento in eventi:

        fixture = evento.get(
            "fixture",
            {}
        )

        teams = evento.get(
            "teams",
            {}
        )

        goals = evento.get(
            "goals",
            {}
        )


        squadra_casa = (

            teams
            .get(
                "home",
                {}
            )
            .get(
                "id"
            )

        )


        squadra_trasferta = (

            teams
            .get(
                "away",
                {}
            )
            .get(
                "id"
            )

        )


        if (
            squadra_casa != team_id
            and
            squadra_trasferta != team_id
        ):

            continue


        stato = (

            fixture
            .get(
                "status",
                {}
            )
            .get(
                "short",
                ""
            )

        )


        if stato not in (

            "FT",
            "AET",
            "PEN"

        ):

            continue


        gol_casa = goals.get(
            "home"
        )

        gol_trasferta = goals.get(
            "away"
        )


        if gol_casa is None:

            continue


        if gol_trasferta is None:

            continue


        if squadra_casa == team_id:

            gol_fatti = gol_casa

            gol_subiti = gol_trasferta

            casa = True

        else:

            gol_fatti = gol_trasferta

            gol_subiti = gol_casa

            casa = False


        if gol_fatti > gol_subiti:

            risultato = "V"

        elif gol_fatti == gol_subiti:

            risultato = "N"

        else:

            risultato = "P"


        risultati.append({

            "risultato":
                risultato,

            "gol_fatti":
                gol_fatti,

            "gol_subiti":
                gol_subiti,

            "casa":
                casa,

            "data":
                fixture.get(
                    "date",
                    ""
                )

        })


    risultati.sort(

        key=lambda x:
        x.get(
            "data",
            ""
        ),

        reverse=True

    )


    risultati = risultati[
        :NUMERO_PARTITE_FORM
    ]


    cache_set(
        chiave,
        risultati
    )


    return risultati


# ============================================================
# CALCOLO STATISTICHE
# ============================================================

def calcola_statistiche(
    partite
):

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

        1
        for p in partite

        if p["risultato"] == "V"

    )


    pareggi = sum(

        1
        for p in partite

        if p["risultato"] == "N"

    )


    sconfitte = sum(

        1
        for p in partite

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


    numero = len(
        partite
    )


    over15 = sum(

        1
        for p in partite

        if (
            p["gol_fatti"]
            + p["gol_subiti"]
        ) >= 2

    )


    over25 = sum(

        1
        for p in partite

        if (
            p["gol_fatti"]
            + p["gol_subiti"]
        ) >= 3

    )


    gol = sum(

        1
        for p in partite

        if (
            p["gol_fatti"] > 0
            and
            p["gol_subiti"] > 0
        )

    )


    punti = (
        vittorie * 3
        + pareggi
    )


    return {

        "partite":
            numero,

        "vittorie":
            vittorie,

        "pareggi":
            pareggi,

        "sconfitte":
            sconfitte,

        "gol_fatti":
            gol_fatti,

        "gol_subiti":
            gol_subiti,

        "media_gol_fatti":
            gol_fatti / numero,

        "media_gol_subiti":
            gol_subiti / numero,

        "punti_media":
            punti / numero,

        "forma":
            "".join(
                p["risultato"]
                for p in partite[:5]
            ),

        "over15":
            over15,

        "over25":
            over25,

        "gol":
            gol

    }


# ============================================================
# RECUPERA PRONOSTICO API-FOOTBALL
# ============================================================

def recupera_pronostico_api(
    fixture_id
):

    print(
        f"🔮 Recupero pronostico "
        f"fixture {fixture_id}",
        flush=True
    )


    dati = api_get(

        "predictions",

        {
            "fixture":
                fixture_id
        }

    )


    if not dati:

        return None


    risposta = dati.get(
        "response",
        []
    )


    if not risposta:

        print(
            "⚠️ Nessun pronostico API-Football.",
            flush=True
        )

        return None


    return risposta[0]


# ============================================================
# PRONOSTICO STATISTICO DI RISERVA
# ============================================================

def genera_pronostico_statistico(
    stats_casa,
    stats_trasferta
):

    if (
        stats_casa["partite"] == 0
        or
        stats_trasferta["partite"] == 0
    ):

        return {

            "esito":
                "N/D",

            "over25":
                "N/D",

            "gol":
                "N/D",

            "confidence":
                0,

            "motivazione":
                "Dati statistici insufficienti."

        }


    forza_casa = (

        stats_casa["punti_media"] * 10

        + stats_casa["media_gol_fatti"] * 3

        - stats_casa["media_gol_subiti"] * 2

        + 2

    )


    forza_trasferta = (

        stats_trasferta["punti_media"] * 10

        + stats_trasferta["media_gol_fatti"] * 3

        - stats_trasferta["media_gol_subiti"] * 2

    )


    differenza = (

        forza_casa
        - forza_trasferta

    )


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


    if (

        stats_casa["media_gol_fatti"] >= 1.1

        and

        stats_trasferta["media_gol_fatti"] >= 1.0

    ):

        gol = "GOL"

    else:

        gol = "NO GOL"


    confidence = 50


    if abs(differenza) >= 3:

        confidence += 15

    elif abs(differenza) >= 1.5:

        confidence += 10


    if media_gol_totale >= 2.5:

        confidence += 5


    if (

        stats_casa["partite"] >= 5

        and

        stats_trasferta["partite"] >= 5

    ):

        confidence += 5


    confidence = min(
        confidence,
        85
    )


    return {

        "esito":
            esito,

        "over25":
            over25,

        "gol":
            gol,

        "confidence":
            confidence,

        "motivazione":
            "Pronostico statistico basato "
            "sulla forma recente."

    }


# ============================================================
# INTERPRETA PRONOSTICO API-FOOTBALL
# ============================================================

def interpreta_pronostico(
    dati_api,
    stats_casa,
    stats_trasferta
):

    if not dati_api:

        return genera_pronostico_statistico(
            stats_casa,
            stats_trasferta
        )


    predictions = dati_api.get(
        "predictions",
        {}
    )


    winner = predictions.get(
        "winner",
        {}
    )


    esito_api = winner.get(
        "name"
    )


    percentuali = predictions.get(
        "percent"
    ) or {}


    percentuale_casa = percentuali.get(
        "home"
    )

    percentuale_pareggio = percentuali.get(
        "draw"
    )

    percentuale_trasferta = percentuali.get(
        "away"
    )


    consiglio = predictions.get(
        "advice"
    )


    under_over = predictions.get(
        "under_over"
    )


    gol = predictions.get(
        "goals",
        {}
    )


    gol_casa = gol.get(
        "home"
    )

    gol_trasferta = gol.get(
        "away"
    )


    # ========================================================
    # ESITO
    # ========================================================

    if esito_api:

        esito_lower = (
            str(esito_api)
            .lower()
        )


        if "home" in esito_lower:

            esito = "1"

        elif "away" in esito_lower:

            esito = "2"

        elif "draw" in esito_lower:

            esito = "X"

        else:

            esito = esito_api

    else:

        fallback = genera_pronostico_statistico(

            stats_casa,
            stats_trasferta

        )

        esito = fallback["esito"]


    # ========================================================
    # OVER / UNDER
    # ========================================================

    if under_over:

        over25 = str(
            under_over
        ).upper()

    else:

        media_totale = (

            stats_casa["media_gol_fatti"]

            + stats_casa["media_gol_subiti"]

            + stats_trasferta["media_gol_fatti"]

            + stats_trasferta["media_gol_subiti"]

        ) / 2


        if media_totale >= 2.7:

            over25 = "OVER 2.5"

        elif media_totale >= 2.2:

            over25 = "OVER 1.5"

        else:

            over25 = "UNDER 2.5"


    # ========================================================
    # GOL / NO GOL
    # ========================================================

    gol_api = predictions.get(
        "both_teams_score"
    )


    if gol_api is True:

        gol = "GOL"

    elif gol_api is False:

        gol = "NO GOL"

    else:

        if (

            stats_casa["media_gol_fatti"] >= 1.1

            and

            stats_trasferta["media_gol_fatti"] >= 1.0

        ):

            gol = "GOL"

        else:

            gol = "NO GOL"


    # ========================================================
    # AFFIDABILITÀ
    # ========================================================

    confidence = 55


    valori_percentuali = []


    for valore in (

        percentuale_casa,
        percentuale_pareggio,
        percentuale_trasferta

    ):

        if valore:

            try:

                valore_numero = float(

                    str(valore)
                    .replace("%", "")
                    .strip()

                )


                valori_percentuali.append(
                    valore_numero
                )


            except Exception:

                pass


    if valori_percentuali:

        confidence = int(

            max(
                valori_percentuali
            )

        )


    confidence = max(

        30,

        min(
            confidence,
            90
        )

    )


    # ========================================================
    # MOTIVAZIONE
    # ========================================================

    motivazione_parts = []


    if consiglio:

        motivazione_parts.append(
            str(consiglio)
        )


    if percentuale_casa:

        motivazione_parts.append(
            f"1: {percentuale_casa}"
        )


    if percentuale_pareggio:

        motivazione_parts.append(
            f"X: {percentuale_pareggio}"
        )


    if percentuale_trasferta:

        motivazione_parts.append(
            f"2: {percentuale_trasferta}"
        )


    if (
        gol_casa
        or
        gol_trasferta
    ):

        motivazione_parts.append(

            "Gol previsti: "
            f"{gol_casa or '?'}-"
            f"{gol_trasferta or '?'}"

        )


    motivazione = " | ".join(
        motivazione_parts
    )


    if not motivazione:

        motivazione = (

            "Pronostico elaborato "
            "sulla base dei dati disponibili."

        )


    return {

        "esito":
            esito,

        "over25":
            over25,

        "gol":
            gol,

        "confidence":
            confidence,

        "motivazione":
            motivazione,

        "percentuale_casa":
            percentuale_casa,

        "percentuale_pareggio":
            percentuale_pareggio,

        "percentuale_trasferta":
            percentuale_trasferta,

        "consiglio":
            consiglio,

        "gol_casa":
            gol_casa,

        "gol_trasferta":
            gol_trasferta

    }


# ============================================================
# ANALISI COMPLETA DELLA PARTITA
# ============================================================

def analizza_partita(
    partita
):

    casa = partita["casa"]

    trasferta = partita["trasferta"]


    casa_id = partita.get(
        "casa_id"
    )

    trasferta_id = partita.get(
        "trasferta_id"
    )


    print(
        "==========================================",
        flush=True
    )


    print(
        f"📊 ANALISI: "
        f"{casa} - {trasferta}",
        flush=True
    )


    form_casa = recupera_form_squadra(
        casa_id
    )


    form_trasferta = recupera_form_squadra(
        trasferta_id
    )


    stats_casa = calcola_statistiche(
        form_casa
    )


    stats_trasferta = calcola_statistiche(
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


    dati_api = recupera_pronostico_api(
        partita["id"]
    )


    pronostico = interpreta_pronostico(

        dati_api,

        stats_casa,

        stats_trasferta

    )


    return {

        "partita":
            partita,

        "stats_casa":
            stats_casa,

        "stats_trasferta":
            stats_trasferta,

        "pronostico":
            pronostico

    }


# ============================================================
# FORMATTA PRONOSTICO
# ============================================================

def formatta_pronostico(
    analisi
):

    pronostico = analisi[
        "pronostico"
    ]


    stats_casa = analisi[
        "stats_casa"
    ]


    stats_trasferta = analisi[
        "stats_trasferta"
    ]


    esito = pronostico.get(
        "esito",
        "N/D"
    )


    gol = pronostico.get(
        "gol",
        "N/D"
    )


    over25 = pronostico.get(
        "over25",
        "N/D"
    )


    confidence = pronostico.get(
        "confidence",
        0
    )


    testo = (

        "🔮 <b>PRONOSTICO</b>\n"

        f"🎯 <b>Esito:</b> {esito}\n"

        f"⚽ <b>Gol:</b> {gol}\n"

        f"📈 <b>Totale:</b> {over25}\n"

        f"💯 <b>Affidabilità:</b> "
        f"{confidence}%"

    )


    pc = pronostico.get(
        "percentuale_casa"
    )

    px = pronostico.get(
        "percentuale_pareggio"
    )

    pt = pronostico.get(
        "percentuale_trasferta"
    )


    if (
        pc
        or px
        or pt
    ):

        testo += (

            "\n\n📊 <b>Probabilità:</b>\n"

            f"1: {pc or 'N/D'}\n"

            f"X: {px or 'N/D'}\n"

            f"2: {pt or 'N/D'}"

        )


    testo += (

        "\n\n📈 <b>FORMA RECENTE</b>\n"

        f"🏠 {stats_casa['forma'] or 'N/D'}\n"

        f"✈️ {stats_trasferta['forma'] or 'N/D'}"

    )


    motivazione = pronostico.get(
        "motivazione"
    )


    if motivazione:

        testo += (

            "\n\n💡 <b>Analisi:</b>\n"

            f"{motivazione}"

        )


    gol_casa = pronostico.get(
        "gol_casa"
    )

    gol_trasferta = pronostico.get(
        "gol_trasferta"
    )


    if (
        gol_casa is not None
        or
        gol_trasferta is not None
    ):

        testo += (

            "\n\n⚽ <b>Gol previsti:</b> "

            f"{gol_casa or '?'}-"
            f"{gol_trasferta or '?'}"

        )


    return testo


# ============================================================
# CREA REPORT
# ============================================================

def crea_report(
    nome_campionato,
    league_id
):

    print(
        "==========================================",
        flush=True
    )


    print(
        f"🚨 CREAZIONE REPORT: "
        f"{nome_campionato}",
        flush=True
    )


    partite = recupera_partite(
        league_id
    )


    if not partite:

        return (

            f"⚽ <b>{nome_campionato}</b>\n\n"

            "⚠️ <b>CALENDARIO NON DISPONIBILE</b>\n\n"

            "L'API-Football sta rifiutando "
            "l'accesso alle partite future "
            "della stagione corrente con "
            "il piano Free.\n\n"

            "🔧 Il bot Telegram e il server "
            "Render funzionano correttamente.\n\n"

            "📡 Il problema riguarda esclusivamente "
            "l'accesso ai dati del calendario "
            "tramite il piano API attuale.\n\n"

            "💡 Per ottenere le prossime partite "
            "reali della stagione corrente serve "
            "un piano API-Football che permetta "
            "l'accesso alla stagione corrente "
            "oppure una diversa sorgente dati."

        )


    messaggio = [

        f"⚽ <b>{nome_campionato}</b>",

        "",

        "🔮 <b>PRONOSTICI CALCIO</b>",

        "",

        "⚠️ Pronostici statistici: "
        "non costituiscono garanzia "
        "del risultato."

    ]


    for indice, partita in enumerate(

        partite,

        start=1

    ):

        print(

            f"🏁 Analisi "
            f"{indice}/{len(partite)}: "
            f"{partita['casa']} - "
            f"{partita['trasferta']}",

            flush=True

        )


        try:

            analisi = analizza_partita(
                partita
            )


            data = formatta_data(
                partita["data"]
            )


            messaggio.append(
                ""
            )


            messaggio.append(

                f"📅 <b>{data}</b>\n"

                f"🏠 <b>{partita['casa']}</b>\n"

                f"✈️ <b>{partita['trasferta']}</b>\n\n"

                f"{formatta_pronostico(analisi)}"

            )


            messaggio.append(
                "━━━━━━━━━━━━━━━━━━"
            )


        except Exception as e:

            print(

                f"❌ Errore analisi "
                f"{partita['id']}: {e}",

                flush=True

            )


            messaggio.append(

                f"📅 <b>"
                f"{formatta_data(partita['data'])}"
                f"</b>\n"

                f"🏠 {partita['casa']}\n"

                f"✈️ {partita['trasferta']}\n\n"

                "❌ Pronostico non disponibile."

            )


            messaggio.append(
                "━━━━━━━━━━━━━━━━━━"
            )


    messaggio.append(
        ""
    )


    messaggio.append(

        "🤖 <i>Elaborazione automatica "
        "tramite API-Football.</i>"

    )


    return "\n".join(
        messaggio
    )


# ============================================================
# DIVISIONE MESSAGGI TELEGRAM
# ============================================================

def dividi_messaggio(
    testo,
    limite=3900
):

    if len(testo) <= limite:

        return [testo]


    blocchi = []

    rimanente = testo


    while len(rimanente) > limite:

        posizione = rimanente.rfind(

            "\n━━━━━━━━━━━━━━━━━━",

            0,

            limite

        )


        if posizione <= 0:

            posizione = rimanente.rfind(

                "\n",

                0,

                limite

            )


        if posizione <= 0:

            posizione = limite


        blocchi.append(

            rimanente[
                :posizione
            ]

        )


        rimanente = (

            rimanente[
                posizione:
            ]
            .lstrip()

        )


    if rimanente:

        blocchi.append(
            rimanente
        )


    return blocchi


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def comando_start(
    message
):

    print(

        f"📩 /start ricevuto "
        f"da chat {message.chat.id}",

        flush=True

    )


    testo = (

        "⚽ <b>BOT PRONOSTICI CALCIO</b>\n\n"

        "Benvenuto! 👋\n\n"

        "Posso mostrarti le prossime "
        "partite dei principali campionati "
        "e creare pronostici statistici.\n\n"

        "📋 Usa <b>/campionati</b> "
        "per scegliere il campionato.\n\n"

        "❓ Usa <b>/help</b> "
        "per vedere i comandi disponibili."

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
def comando_help(
    message
):

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
def comando_campionati(
    message
):

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
# RICONOSCE IL CAMPIONATO
# ============================================================

def trova_campionato(
    testo
):

    testo = (

        testo
        .strip()
        .lower()

    )


    for nome, dati in CAMPIONATI.items():

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


        if testo == nome_pulito:

            return (

                nome,

                dati["id"]

            )


    return (
        None,
        None
    )


# ============================================================
# GESTIONE MESSAGGI
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def gestione_messaggio(
    message
):

    if not message.text:

        return


    testo_utente = (

        message.text
        .strip()
        .lower()

    )


    nome_campionato, league_id = (

        trova_campionato(
            testo_utente
        )

    )


    if not nome_campionato:

        bot.send_message(

            message.chat.id,

            "❌ Campionato non riconosciuto.\n\n"

            "Usa <b>/campionati</b> "
            "per vedere quelli disponibili.",

            parse_mode="HTML"

        )

        return


    print(

        f"📨 RICHIESTA CAMPIONATO: "
        f"{nome_campionato}",

        flush=True

    )


    messaggio_attesa = bot.send_message(

        message.chat.id,

        f"⏳ Analizzo "
        f"<b>{nome_campionato}</b>...\n\n"

        "Sto verificando il calendario "
        "e i dati disponibili.\n\n"

        "Attendi qualche secondo.",

        parse_mode="HTML"

    )


    try:

        report = crea_report(

            nome_campionato,

            league_id

        )


        print(

            "📨 Report finale creato.",

            flush=True

        )


        print(

            f"📏 Lunghezza report: "
            f"{len(report)} caratteri",

            flush=True

        )


        blocchi = dividi_messaggio(
            report
        )


        for indice, blocco in enumerate(

            blocchi,

            start=1

        ):

            print(

                f"📤 Invio messaggio "
                f"{indice}/{len(blocchi)} ...",

                flush=True

            )


            bot.send_message(

                message.chat.id,

                blocco,

                parse_mode="HTML"

            )


            print(

                f"✅ Messaggio "
                f"{indice}/{len(blocchi)} inviato.",

                flush=True

            )


        try:

            bot.delete_message(

                message.chat.id,

                messaggio_attesa.message_id

            )

        except Exception:

            pass


        print(

            "🏁 REPORT TELEGRAM "
            "INVIATO COMPLETAMENTE.",

            flush=True

        )


    except Exception as e:

        print(

            f"❌ Errore creazione report: "
            f"{e}",

            flush=True

        )


        try:

            bot.delete_message(

                message.chat.id,

                messaggio_attesa.message_id

            )

        except Exception:

            pass


        bot.send_message(

            message.chat.id,

            "❌ Si è verificato un errore "
            "durante il recupero dei pronostici.\n\n"

            "Riprova tra qualche minuto."

        )


# ============================================================
# SERVER HTTP RENDER + WEBHOOK
# ============================================================

class HealthHandler(
    http.server.BaseHTTPRequestHandler
):


    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(
        self
    ):

        print(

            f"🌐 GET ricevuta: "
            f"{self.path}",

            flush=True

        )


        if self.path == "/health":

            risposta = (
                "OK - Bot pronostici calcio online"
            )

        else:

            risposta = (
                "Bot pronostici calcio online!"
            )


        self.send_response(
            200
        )


        self.send_header(

            "Content-Type",

            "text/plain; charset=utf-8"

        )


        self.send_header(

            "Content-Length",

            str(

                len(

                    risposta.encode(
                        "utf-8"
                    )

                )

            )

        )


        self.end_headers()


        self.wfile.write(

            risposta.encode(
                "utf-8"
            )

        )


    # --------------------------------------------------------
    # POST WEBHOOK TELEGRAM
    # --------------------------------------------------------

    def do_POST(
        self
    ):

        print(

            f"📩 POST RICEVUTA: "
            f"{self.path}",

            flush=True

        )


        if self.path != WEBHOOK_PATH:

            print(

                f"❌ Percorso webhook "
                f"non corretto: {self.path}",

                flush=True

            )


            self.send_response(
                404
            )

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

                body.decode(
                    "utf-8"
                )

            )


            update = (

                telebot.types.Update
                .de_json(data)

            )


            print(

                "✅ Update Telegram "
                "decodificato.",

                flush=True

            )


            self.send_response(
                200
            )


            self.send_header(

                "Content-Type",

                "text/plain; charset=utf-8"

            )


            self.end_headers()


            self.wfile.write(
                b"OK"
            )


            thread = threading.Thread(

                target=elabora_update,

                args=(update,),

                daemon=True

            )


            thread.start()


            print(

                "✅ Update inviato "
                "al thread di elaborazione.",

                flush=True

            )


        except Exception as e:

            print(

                f"❌ ERRORE WEBHOOK: {e}",

                flush=True

            )


            try:

                self.send_response(
                    200
                )

                self.end_headers()

                self.wfile.write(
                    b"ERROR"
                )

            except Exception:

                pass


    # --------------------------------------------------------
    # DISATTIVA LOG HTTP STANDARD
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

def elabora_update(
    update
):

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

            f"❌ Errore elaborazione update: "
            f"{e}",

            flush=True

        )


# ============================================================
# AVVIA SERVER HTTP
# ============================================================

def avvia_server():

    server = (

        http.server.ThreadingHTTPServer(

            (
                "0.0.0.0",
                PORT
            ),

            HealthHandler

        )

    )


    print(

        f"🌐 Server HTTP avviato "
        f"sulla porta {PORT}",

        flush=True

    )


    print(

        f"🔗 Webhook Telegram: "
        f"{WEBHOOK_URL}",

        flush=True

    )


    server.serve_forever()


# ============================================================
# CONFIGURA WEBHOOK TELEGRAM
# ============================================================

def configura_webhook():

    print(
        "==========================================",
        flush=True
    )

    print(
        "📡 CONFIGURAZIONE WEBHOOK TELEGRAM",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )


    try:

        print(
            "🔎 Verifica bot Telegram...",
            flush=True
        )


        me = bot.get_me()


        print(

            f"🤖 BOT TELEGRAM: "
            f"@{me.username}",

            flush=True

        )


        print(

            f"🆔 BOT ID: "
            f"{me.id}",

            flush=True

        )


        print(
            "🔗 Impostazione webhook:",
            flush=True
        )


        print(
            WEBHOOK_URL,
            flush=True
        )


        risultato = bot.set_webhook(

            url=WEBHOOK_URL,

            allowed_updates=[
                "message"
            ],

            drop_pending_updates=True

        )


        print(

            f"✅ Webhook impostato: "
            f"{risultato}",

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

            f"Pending updates: "
            f"{info.pending_update_count}",

            flush=True

        )


        print(

            f"Ultimo errore: "
            f"{info.last_error_message}",

            flush=True

        )


        print(

            f"Data ultimo errore: "
            f"{info.last_error_date}",

            flush=True

        )


        print(

            f"IP Telegram: "
            f"{info.ip_address}",

            flush=True

        )


        print(

            f"Max connessioni: "
            f"{info.max_connections}",

            flush=True

        )


        print(

            f"Allowed updates: "
            f"{info.allowed_updates}",

            flush=True

        )


        print(
            "====================================",
            flush=True
        )


        if info.url == WEBHOOK_URL:

            print(

                "✅ WEBHOOK TELEGRAM "
                "CONFIGURATO CORRETTAMENTE.",

                flush=True

            )

            return True


        print(

            "⚠️ Il webhook restituito "
            "da Telegram non coincide "
            "con quello previsto.",

            flush=True

        )


        return False


    except Exception as e:

        print(

            f"❌ ERRORE CONFIGURAZIONE "
            f"WEBHOOK: {e}",

            flush=True

        )


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
    # TEST API-FOOTBALL
    # --------------------------------------------------------

    test_api()


    # --------------------------------------------------------
    # WEBHOOK TELEGRAM
    # --------------------------------------------------------

    webhook_ok = configura_webhook()


    if webhook_ok:

        print(

            "🟢 Telegram configurato correttamente.",

            flush=True

        )

    else:

        print(

            "🔴 ATTENZIONE: webhook Telegram "
            "NON configurato.",

            flush=True

        )


    print(

        "==========================================",

        flush=True

    )


    print(

        "🚀 BOT ONLINE!",

        flush=True

    )


    print(

        f"🌐 Render: {RENDER_EXTERNAL_URL}",

        flush=True

    )


    print(

        f"📡 Webhook: {WEBHOOK_URL}",

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

            threading.Event().wait(
                3600
            )


    except KeyboardInterrupt:

        print(

            "🛑 Arresto bot...",

            flush=True

        )
