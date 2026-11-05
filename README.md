# Data Leak Platform

Repository unificato che raccoglie due progetti complementari per la gestione dei data leak:

1. **Data Leak Reply Manager** (`dataleakmail/`) — servizio per rispondere automaticamente a notifiche di data leak su Microsoft 365, con classificazione dell'intent tramite endpoint NVIDIA e firme S/MIME.
2. **DataLeak NAS Pipeline** (`dataleak-supermanager/`) — pipeline distribuita di worker Python che scaricano file da canali Telegram, li decomprimono, analizzano i dati leak e preparano/inviano email di notifica (tramite RabbitMQ e MySQL).

---

## Struttura del repository

```
.
├── README.md               # questo file
├── requirements.txt        # dipendenze Python unificate (entrambi i progetti)
├── .gitignore
├── dataleakmail/           # Data Leak Reply Manager (M365 + NVIDIA + S/MIME)
│   ├── main.py             # Orchestratore principale
│   ├── config.yaml         # Configurazione (leggende da .env)
│   ├── .env.example        # Esempio variabili d'ambiente
│   ├── requirements.txt    # Dipendenze specifiche di dataleakmail
│   ├── m365/               # Auth + client Microsoft Graph
│   ├── classifier/         # Classificatore NVIDIA
│   ├── actions/            # Runner script esterni (kleak, infostealer)
│   ├── smime/              # Firma S/MIME
│   ├── db/                 # SQLite stati email processate
│   ├── scripts/            # Script esterni kleak.py / infostealer.py
│   ├── signature/          # Firma HTML
│   ├── certs/              # Certificato .p12 (da fornire, non tracciato)
│   └── logs/               # Log applicazione
├── dataleak-supermanager/  # DataLeak NAS Pipeline (Telegram + RabbitMQ + MySQL)
│   ├── download.py                  # Scarica file da canali Telegram
│   ├── download_gofile_links.py     # Estrae link GoFile dai messaggi Telegram
│   ├── password.py                  # Estrae password dagli archivi
│   ├── decompress.py                # Decomprime archivi (7z)
│   ├── process_subdirectories.py    # Esplora alberi di directory estratti
│   ├── analyze.py                   # Estrae email, password, carte, paese, data leak
│   ├── send_mail.py                 # Invia email di notifica in coda
│   ├── config.py                    # Configurazione runtime (NON tracciata, vedi .gitignore)
│   ├── create_tables.sql            # Schema MySQL
│   ├── utils.py                     # Helper condivisi (logging, RabbitMQ, DB, hash, decompressione)
│   ├── password_matrix.json         # Matrice dinamica di password di fallback
│   ├── templates/mail_template_en.html  # Template email HTML
│   ├── tests/                       # Test (unittest)
│   ├── ruff.toml                    # Configurazione lint ruff
│   ├── AGENTS.md                    # Documentazione sviluppatore
│   ├── start_pipeline.sh            # Avvia tutti i worker in tmux
│   └── ...                          # altri worker e script di manutenzione
└── ...
```

---

## 1. Data Leak Reply Manager

Vedi la documentazione completa in `dataleakmail/README.md`.

### Prerequisiti
- Python 3.11+
- OpenSSL (per firma S/MIME)
- Certificato S/MIME in formato `.p12` con password
- App Registration su Microsoft Entra ID con **Application permissions**:
  - `Mail.Read.Application`, `Mail.ReadWrite.Application`, `Mail.Send.Application`
- Consenso amministratore concesso (Grant admin consent)
- API key NVIDIA per endpoint di classificazione (Nemotron Chat)

### Installazione
```bash
cd dataleakmail
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Modificare .env con i propri valori
```

### Esecuzione
```bash
# Modalità normale (polling ciclico)
python main.py

# Modalità test (elabora una singola email)
python main.py --test 1
```

---

## 2. DataLeak NAS Pipeline

Vedi la documentazione completa in `dataleak-supermanager/README.md` e `dataleak-supermanager/AGENTS.md`.

### Flusso principale
`download.py` → `password.py` → `decompress.py` → `process_subdirectories.py` → `analyze.py` → `send_mail.py`

### Dipendenze di sistema
- **p7zip-full** (`sudo apt install p7zip-full`) — per l'estrazione degli archivi
- **MySQL 5.7+** con utf8mb4
- **RabbitMQ** message broker
- **Playwright Chromium** (`python -m playwright install chromium`) — per la risoluzione link GoFile

### Installazione
```bash
cd dataleak-supermanager
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
mysql -u USER -p < create_tables.sql

# Creare config.py (non tracciata) con DB, RabbitMQ, Telegram, SMTP
```

### Esecuzione
```bash
# Avvia tutti i worker in tmux
./start_pipeline.sh

# Oppure esegui i worker singolarmente
python download.py
python analyze.py
python send_mail.py 100
```

---

## Configurazione e sicurezza

⚠️ **Importante**: il file `dataleak-supermanager/config.py` nella versione originale conteneva credenziali hardcodate (password RabbitMQ/MySQL, chiavi Telethon, password SMTP, ecc.). Questo file è ora escluso dal tracciamento git (`.gitignore`) e non deve essere committato. Le credenziali vanno inserite via variabili d'ambiente o in un file `config.py` locale non tracciato.

Il certificato S/MIME `certs/smime.p12` di `dataleakmail` non è tracciato: va fornito dall'utente.

---

## Licenza

Vedi i singoli sottoprogetti.
