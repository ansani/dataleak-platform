"""Esecuzione script esterni kleak e infostealer."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def run_kleak(email_address: str, script_name: str = "kleak.py") -> str:
    """Esegue lo script kleak.py passando l'email come argomento CLI.

    Restituisce l'output stdout dello script come stringa.
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Script kleak non trovato: {script_path}")

    cmd = [sys.executable, str(script_path), email_address]
    logger.info("Esecuzione kleak: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        logger.error("kleak.py fallito (codice %d): %s", result.returncode, result.stderr)
        raise RuntimeError(f"kleak.py fallito: {result.stderr}")

    return result.stdout.strip()


def run_infostealer(email_address: str, script_name: str = "infostealer.py") -> str:
    """Esegue lo script infostealer.py passando l'email come argomento CLI.

    Restituisce l'output stdout dello script come stringa.
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Script infostealer non trovato: {script_path}")

    cmd = [sys.executable, str(script_path), email_address]
    logger.info("Esecuzione infostealer: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        logger.error("infostealer.py fallito (codice %d): %s", result.returncode, result.stderr)
        raise RuntimeError(f"infostealer.py fallito: {result.stderr}")

    return result.stdout.strip()