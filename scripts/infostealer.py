#!/usr/bin/env python3
"""Placeholder per lo script infostealer.

Sostituire questo file con il proprio script. Deve accettare l'email
come primo argomento CLI e stampare su stdout il device vittima.
"""

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python infostealer.py <email>", file=sys.stderr)
        sys.exit(1)

    email = sys.argv[1]
    # TODO: implementare la logica infostealer per l'email fornita
    print(f"Risultato infostealer per {email}: (placeholder - sostituire con output reale)")


if __name__ == "__main__":
    main()