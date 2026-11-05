"""Esecuzione script esterni kleak e infostealer.

Gli script possono risiedere in qualsiasi directory. Per eseguirli correttamente:

- Imposta ``KLEAK_SCRIPT_PATH`` e ``INFOSTEALER_SCRIPT_PATH`` con il percorso
  assoluto (o relativo alla working directory) dei tuoi script.
- Imposta ``KLEAK_PYTHON`` e ``INFOSTEALER_PYTHON`` con il percorso
  dell'eseguibile Python del loro virtualenv (es. ``/percorso/.venv/bin/python``).
  Se omesso, si usa l'interprete Python corrente.

Gli script vengono eseguiti con ``cwd`` impostato alla loro directory
di appartenenza, in modo che possano leggere il proprio file di
configurazione presente nella stessa cartella.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Directory degli script placeholder (fallback se non specificata via env)
DEFAULT_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _resolve_script_path(env_var: str, default_path: Path) -> Path:
    """Risolve il percorso dello script da variabile d'ambiente o default."""
    env_val = os.getenv(env_var)
    if env_val:
        p = Path(env_val)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve()
    return default_path.resolve()


def _resolve_python(env_var: str, default_python: str) -> str:
    """Risolve l'eseguibile Python da usare."""
    env_val = os.getenv(env_var)
    if env_val:
        return env_val
    return default_python


def run_kleak(email_address: str, script_name: str = "kleak.py") -> str:
    """Esegue lo script kleak passando l'email come argomento CLI."""
    script_path = _resolve_script_path("KLEAK_SCRIPT_PATH", DEFAULT_SCRIPTS_DIR / "kleak.py")
    python_exec = _resolve_python("KLEAK_PYTHON", sys.executable)

    if not script_path.exists():
        raise FileNotFoundError(f"Script kleak non trovato: {script_path}")

    cmd = [python_exec, str(script_path), email_address]
    logger.info("Esecuzione kleak: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(script_path.parent),
    )

    if result.returncode != 0:
        logger.error("kleak.py fallito (codice %d): %s", result.returncode, result.stderr)
        raise RuntimeError(f"kleak.py fallito: {result.stderr}")

    return result.stdout.strip()


def run_infostealer(email_address: str, script_name: str = "infostealer.py") -> str:
    """Esegue lo script infostealer passando l'email come argomento CLI."""
    script_path = _resolve_script_path(
        "INFOSTEALER_SCRIPT_PATH", DEFAULT_SCRIPTS_DIR / "infostealer.py"
    )
    python_exec = _resolve_python("INFOSTEALER_PYTHON", sys.executable)

    if not script_path.exists():
        raise FileNotFoundError(f"Script infostealer non trovato: {script_path}")

    cmd = [python_exec, str(script_path), email_address]
    logger.info("Esecuzione infostealer: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(script_path.parent),
    )

    if result.returncode != 0:
        logger.error("infostealer.py fallito (codice %d): %s", result.returncode, result.stderr)
        raise RuntimeError(f"infostealer.py fallito: {result.stderr}")

    return result.stdout.strip()