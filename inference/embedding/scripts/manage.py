#!/usr/bin/env python3
"""One small interactive entry point for routine LightModel Gateway operations."""
from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_DIR / "scripts"


def run(script: str, *arguments: str) -> None:
    subprocess.run([str(SCRIPTS / script), *arguments], cwd=PROJECT_DIR, check=False)


def main() -> int:
    actions = {
        "1": ("Add an OpenAI-compatible model", lambda: run("modelctl.py", "add-openai")),
        "2": ("Create a customer API key", lambda: run("keyctl.py", "create")),
        "3": ("Top up a customer API key", None),
        "4": ("Show service status", lambda: run("status.sh")),
        "5": ("Start service", lambda: run("start.sh")),
        "6": ("Stop service", lambda: run("stop.sh")),
        "7": ("List models and customer balances", None),
    }
    while True:
        print("\nLightModel Gateway")
        for number, (label, _) in actions.items():
            print(f"  {number}. {label}")
        print("  0. Exit")
        choice = input("Choose an action: ").strip()
        if choice == "0":
            return 0
        if choice == "3":
            name = input("Key name: ").strip()
            usd = input("USD to add: ").strip()
            if name and usd:
                run("keyctl.py", "topup", name, "--usd", usd)
            else:
                print("Key name and USD amount are required.")
            continue
        if choice == "7":
            run("modelctl.py", "list")
            run("keyctl.py", "list")
            continue
        action = actions.get(choice)
        if action is None:
            print("Unknown choice.")
        else:
            action[1]()


if __name__ == "__main__":
    raise SystemExit(main())
