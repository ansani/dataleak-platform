# Data Leak Reply Manager

Servizio per gestire risposte automatiche a notifiche di data leak su Microsoft 365,
con classificazione intent tramite endpoint NVIDIA e firme S/MIME.

## Struttura

```
├── main.py                # Orchestratore principale
├── config.yaml            # Configurazione (leggende da .env)
├── .env.example           # Esempio variabili d'ambiente
├── requirements.txt       # Dipendenze Python
├── .gitignore
├── m365/                  # Auth + client Microsoft Graph
├── classifier/            # Classificatore NVIDIA
├── actions/               # Runner script esterni (kleak, infostealer)
├── smime/                 # Firma S/MIME
├── db/                    # SQLite stati email processate
├── scripts/               # Script esterni kleak.py / infostealer.py
├── signature/             # Firma HTML
├── certs/                 # Certificato .p12 (da fornire)
└── logs/                  # Log applicazione
```

## Prerequisiti

- Python 3.11+
- OpenSSL installato (per firma S/MIME)
- Certificato S/MIME in formato `.p12` con password
- App Registration su Microsoft Entra ID con **Application permissions** (non delegated):
  - `Mail.Read.Application`
  - `Mail.ReadWrite.Application`
  - `Mail.Send.Application`
- Concessione del consenso amministratore (Grant admin consent) per i permessi sopra
- API key NVIDIA per endpoint di classificazione (Nemotron Chat, es. nemotron-3.1-8b-instruct)

## Installazione

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Modificare .env con i propri valori
```

## Esecuzione

### Modalità normale (polling ciclico)

L'autenticazione usa il **client credentials flow** (app-only): nessun login interattivo è necessario. Il token viene ottenuto automaticamente al primo avvio.

```bash
python main.py
```

Il servizio esegue polling ciclico della casella in arrivo (default ogni 5 minuti).

### Modalità test (--test)

Per testare il flusso su una singola email senza attivare il polling:

```bash
# Elabora l'email più recente (1 = più recente, 2 = penultima, ecc.)
python main.py --test 1
```

In modalità test:
- Viene letta l'email NUMERO contando dalla più recente
- Vengono eseguite tutte le analisi (classificazione NVIDIA, script kleak/infostealer se richiesto, firma S/MIME)
- La risposta viene inviata a **salvatoreansani@gmail.com** (indipendentemente dal mittente originale)
- Viene ignorato lo stato "già processata" nel database, permettendo riesecuzioni

## Variabili d'ambiente (.env)

| Variabile | Descrizione |
|-----------|-------------|
| `M365_TENANT_ID` | ID tenant Microsoft Entra ID |
| `M365_CLIENT_ID` | ID client App Registration |
| `M365_CLIENT_SECRET` | Segreto client App Configuration (richiesto) |
| `M365_USERNAME` | Casella di posta da monitorare |
| `NVIDIA_API_KEY` | Chiave API NVIDIA |
| `NVIDIA_ENDPOINT_URL` | Endpoint NVIDIA chat completions (es. https://integrate.api.nvidia.com/v1/chat/completions) |
| `NVIDIA_MODEL` | Modello NVIDIA chat (es. nvidia/nemotron-3.5-bolt-e4b) |
| `SMIME_CERT_PATH` | Percorso file .p12 |
| `SMIME_CERT_PASSWORD` | Password del .p12 |
| `SIGNATURE_HTML_PATH` | Percorso file firma HTML |
| `DB_PATH` | Percorso database SQLite |

## Note

- Gli script `kleak.py` e `infostealer.py` in `scripts/` sono placeholder: sostituirli con i propri script.
- La firma S/MIME viene generata in formato `multipart/signed` (PEM) tramite OpenSSL.
- Ogni email processata viene tracciata in SQLite per evitare doppi invii.
- **Invio S/MIME tramite Graph API**: il servizio invia il messaggio firmato S/MIME utilizzando l'endpoint `sendMail` di Graph API con il MIME completo nel corpo della richiesta (`Content-Type: text/plain`), come documentato da Microsoft: https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0. Non è necessario configurare SMTP.
- **Limitazione nota**: Microsoft Exchange transport potrebbe riprocessare il messaggio MIME e rimuovere il wrapper `multipart/signed` su alcuni tenant. Se la firma S/MIME non risulta valida al destinatario, verificare con l'amministratore M365 che il tenant supporti l'invio di messaggi MIME pre-composti tramite Graph API.