#!/usr/bin/env python3
"""Minimal, interactive model allow-list manager for LightModel Gateway."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_CONFIG = PROJECT_DIR / "gateway_models.json"
SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def load_config() -> dict[str, object]:
    try:
        config = json.loads(MODEL_CONFIG.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {MODEL_CONFIG}: {error}") from error
    if not isinstance(config, dict) or not isinstance(config.get("models"), list):
        raise ValueError("gateway_models.json must contain a models list")
    return config


def save_config(config: dict[str, object]) -> None:
    temporary = MODEL_CONFIG.with_suffix(".json.new")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, MODEL_CONFIG)


def decimal_price(prompt: str, default: str) -> str:
    while True:
        value = input(f"{prompt} [{default}]: ").strip() or default
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            print("Enter a non-negative decimal amount.")
            continue
        if parsed.is_finite() and parsed >= 0 and parsed.as_tuple().exponent >= -6:
            return f"{parsed:f}"
        print("Enter a non-negative amount with at most six decimal places.")


def valid_model_id(prompt: str, default: str) -> str:
    while True:
        value = input(f"{prompt} [{default}]: ").strip() or default
        if SAFE_MODEL_ID.fullmatch(value):
            return value
        print("Use letters, numbers, dots, underscores, or dashes (up to 128 characters).")


def add_openai() -> int:
    config = load_config()
    models = config["models"]
    assert isinstance(models, list)
    default_model = "gpt-4.1-mini"
    public_id = valid_model_id("Public model ID", default_model)
    if any(isinstance(item, dict) and item.get("id") == public_id for item in models):
        raise ValueError(f"model '{public_id}' already exists")
    upstream_model = input(f"Upstream model ID [{public_id}]: ").strip() or public_id
    base_url = input("Upstream base URL [https://api.openai.com/v1]: ").strip() or "https://api.openai.com/v1"
    if not base_url.startswith(("https://", "http://")) or "?" in base_url or "#" in base_url:
        raise ValueError("base URL must be an absolute HTTP(S) URL without query or fragment")
    input_price = decimal_price("Input USD per 1M tokens", "0.40")
    output_price = decimal_price("Output USD per 1M tokens", "1.60")
    secret = getpass.getpass("Upstream API key (not displayed or stored in JSON): ").strip()
    if not secret:
        raise ValueError("upstream API key cannot be empty")
    secret_relative_path = f"secrets/{public_id}_upstream_key.txt"
    secret_path = PROJECT_DIR / secret_relative_path
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        secret_path.write_text(secret + "\n")
    finally:
        os.umask(old_umask)
    os.chmod(secret_path, 0o600)
    models.append(
        {
            "id": public_id,
            "provider": "openai_compatible",
            "base_url": base_url.rstrip("/"),
            "api_key_file": secret_relative_path,
            "endpoints": ["chat/completions", "responses"],
            "upstream_model": upstream_model,
            "input_price_per_million_usd": input_price,
            "output_price_per_million_usd": output_price,
            "max_output_tokens": 4096,
            "chat_max_output_parameter": "max_completion_tokens",
        }
    )
    save_config(config)
    print(f"Added '{public_id}'. Reload only the lightweight gateway: ./scripts/gateway_stop.sh && ./scripts/gateway_start.sh")
    return 0


def list_models() -> int:
    config = load_config()
    models = config["models"]
    assert isinstance(models, list)
    print(f"{'MODEL':<26} {'PROVIDER':<18} {'ENDPOINTS':<30} {'PRICE / 1M TOKENS'}")
    for item in models:
        if not isinstance(item, dict):
            continue
        endpoints = ",".join(item.get("endpoints", []))
        prices = f"in ${item.get('input_price_per_million_usd', '0')} / out ${item.get('output_price_per_million_usd', '0')}"
        print(f"{str(item.get('id', '-')):<26} {str(item.get('provider', '-')):<18} {endpoints:<30} {prices}")
    return 0


def remove_model(model_id: str) -> int:
    config = load_config()
    models = config["models"]
    assert isinstance(models, list)
    retained = [item for item in models if not isinstance(item, dict) or item.get("id") != model_id]
    if len(retained) == len(models):
        raise ValueError(f"model '{model_id}' was not found")
    if not retained:
        raise ValueError("refusing to remove the last configured model")
    config["models"] = retained
    save_config(config)
    print(f"Removed '{model_id}' from the allow-list. Its upstream secret file was kept; reload the gateway before serving requests.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LightModel Gateway model configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list configured public models")
    subparsers.add_parser("add-openai", help="interactively add an OpenAI-compatible model")
    remove = subparsers.add_parser("remove", help="remove a model from the allow-list")
    remove.add_argument("model_id")
    args = parser.parse_args()
    try:
        if args.command == "list":
            return list_models()
        if args.command == "add-openai":
            return add_openai()
        return remove_model(args.model_id)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
