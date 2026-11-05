#!/usr/bin/env python3
"""Placeholder per lo script kleak.

Sostituire questo file con il proprio script. Deve accettare l'email
come primo argomento CLI e stampare su stdout l'elenco degli account esposti.
"""

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python kleak.py <email>", file=sys.stderr)
        sys.exit(1)

    email = sys.argv[1]
    # TODO: implementare la logica kleak per l'email fornita
    print(f"Risultato kleak per {email}: (placeholder - sostituire con output reale)")


if __name__ == "__main__":
    main()