#!/usr/bin/env python3
"""Small command-line manager for LightModel Gateway virtual API keys."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quota_store import (
    MICRO_USD_PER_USD,
    add_credit,
    add_quota,
    create_key,
    find_key,
    init_database,
    list_keys,
    rotate_key,
    set_credit,
    set_enabled,
    set_quota,
    usage_logs,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent


def read_config() -> dict[str, str]:
    config: dict[str, str] = {}
    for raw_line in (PROJECT_DIR / "config.env").read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        config[key] = value
    return config


def parse_quota(value: str) -> int | None:
    if value.lower() in {"unlimited", "none", "inf", "无限"}:
        return None
    quota = int(value)
    if quota < 0:
        raise argparse.ArgumentTypeError("token quota must be >= 0 or 'unlimited'")
    return quota


def parse_credit(value: str) -> int | None:
    if value.lower() in {"unlimited", "none", "inf", "无限"}:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("credit must be a decimal USD amount") from error
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("credit must be >= 0 or 'unlimited'")
    scaled = amount * MICRO_USD_PER_USD
    if scaled != scaled.to_integral_value():
        raise argparse.ArgumentTypeError("credit supports at most six decimal places")
    return int(scaled)


def format_usd(microusd: int | None) -> str:
    return "unlimited" if microusd is None else f"${microusd / MICRO_USD_PER_USD:,.6f}"


def remaining_credit(row: sqlite3.Row) -> int | None:
    if not row["credit_enabled"]:
        return None
    if row["credit_microusd"] is None:
        return None
    return max(0, row["credit_microusd"] - row["spent_microusd"] - row["reserved_microusd"])


def remaining_tokens(row: sqlite3.Row) -> int | None:
    if row["quota_tokens"] is None:
        return None
    return max(0, row["quota_tokens"] - row["used_tokens"] - row["reserved_tokens"])


def format_tokens(value: int | None) -> str:
    return "unlimited" if value is None else f"{value:,}"


def format_credit(row: sqlite3.Row, column: str) -> str:
    if not row["credit_enabled"]:
        return "not configured"
    return format_usd(row[column])


def print_key(row: sqlite3.Row) -> None:
    print(f"name: {row['name']}")
    print(f"id: {row['id']}")
    print(f"prefix: {row['key_prefix']}...")
    print(f"enabled: {'yes' if row['enabled'] else 'no'}")
    print(f"credit_usd: {format_credit(row, 'credit_microusd')}")
    print(f"spent_usd: {format_credit(row, 'spent_microusd')}")
    print(f"reserved_usd: {format_credit(row, 'reserved_microusd')}")
    print(f"remaining_usd: {format_usd(remaining_credit(row)) if row['credit_enabled'] else 'not configured'}")
    print(f"legacy_token_quota: {format_tokens(row['quota_tokens'])}")
    print(f"legacy_token_used: {format_tokens(row['used_tokens'])}")
    print(f"legacy_token_remaining: {format_tokens(remaining_tokens(row))}")
    print(f"created_at: {row['created_at']}")
    print(f"last_used_at: {row['last_used_at'] or '-'}")


def build_parser(default_db: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LightModel Gateway virtual API-key manager")
    parser.add_argument("--db", type=Path, default=default_db)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="initialize or migrate the local usage database")

    create = subparsers.add_parser("create", help="create a billable API key")
    create.add_argument("--name", default=argparse.SUPPRESS)
    balance = create.add_mutually_exclusive_group()
    balance.add_argument("--credit-usd", type=parse_credit, default=argparse.SUPPRESS)
    balance.add_argument(
        "--quota-tokens",
        type=parse_quota,
        default=argparse.SUPPRESS,
        help="legacy local-embedding token allowance",
    )
    create.add_argument("--yes", action="store_true", help="skip interactive confirmation")

    import_key = subparsers.add_parser("import", help="import an existing secret as a billable key")
    import_key.add_argument("--name", required=True)
    import_key.add_argument("--key-file", required=True, type=Path)
    balance = import_key.add_mutually_exclusive_group(required=True)
    balance.add_argument("--credit-usd", type=parse_credit)
    balance.add_argument("--quota-tokens", type=parse_quota, help="legacy local-embedding token allowance")

    subparsers.add_parser("list", help="list keys and dollar balances")
    show = subparsers.add_parser("show", help="show one key")
    show.add_argument("name")

    credit = subparsers.add_parser("set-credit", help="set a key's total USD credit")
    credit.add_argument("name")
    credit.add_argument("--credit-usd", required=True, type=parse_credit)
    topup = subparsers.add_parser("topup", help="add USD credit to a key")
    topup.add_argument("name")
    topup.add_argument("--usd", required=True, type=parse_credit)

    # These two are retained solely for existing local-embedding accounts.
    quota = subparsers.add_parser("set-quota", help="set a legacy local token quota")
    quota.add_argument("name")
    quota.add_argument("--quota-tokens", required=True, type=parse_quota)
    add = subparsers.add_parser("add-tokens", help="add to a legacy local token quota")
    add.add_argument("name")
    add.add_argument("--tokens", required=True, type=int)

    for command in ("enable", "disable"):
        toggle = subparsers.add_parser(command, help=f"{command} a key")
        toggle.add_argument("name")
    rotate = subparsers.add_parser("rotate", help="replace a key's secret")
    rotate.add_argument("name")
    logs = subparsers.add_parser("logs", help="show recent per-request billing records")
    logs.add_argument("name")
    logs.add_argument("--limit", type=int, default=20)
    return parser


def main() -> int:
    config = read_config()
    parser = build_parser(PROJECT_DIR / config.get("EMBED_QUOTA_DB", "quota/usage.db"))
    args = parser.parse_args()
    db_path = args.db.resolve()
    init_database(db_path)
    try:
        if args.command == "init":
            print(f"Usage database ready: {db_path}")
        elif args.command == "create":
            has_credit = hasattr(args, "credit_usd")
            has_tokens = hasattr(args, "quota_tokens")
            if args.yes and (
                not hasattr(args, "name") or not (has_credit or has_tokens)
            ):
                parser.error("--yes requires --name and --credit-usd (or legacy --quota-tokens)")
            name = getattr(args, "name", "").strip()
            while not name:
                name = input("Key name: ").strip()
            if not has_credit and not has_tokens:
                while True:
                    try:
                        args.credit_usd = parse_credit(input("Total USD credit (or unlimited): ").strip())
                        has_credit = True
                        break
                    except argparse.ArgumentTypeError as error:
                        print(f"Invalid amount: {error}")
            credit = args.credit_usd if has_credit else None
            tokens = args.quota_tokens if has_tokens else None
            if not args.yes:
                print("\nCreate key:")
                print(f"  name: {name}")
                print(f"  credit: {format_usd(credit)}" if has_credit else f"  legacy token quota: {format_tokens(tokens)}")
                if input("Confirm? [y/N]: ").strip().lower() not in {"y", "yes"}:
                    print("Cancelled; no key was created.")
                    return 0
            api_key, _ = create_key(
                db_path,
                name,
                tokens,
                credit_microusd=credit,
                credit_enabled=has_credit,
            )
            print("API key created. Save this value now; only its hash is stored:")
            print(api_key)
        elif args.command == "import":
            api_key = args.key_file.read_text().strip()
            create_key(
                db_path,
                args.name,
                getattr(args, "quota_tokens", None),
                api_key=api_key,
                credit_microusd=getattr(args, "credit_usd", None),
                credit_enabled=hasattr(args, "credit_usd"),
            )
            print(f"Imported key '{args.name}'; plaintext was not copied to the database.")
        elif args.command == "list":
            print(f"{'NAME':<20} {'STATUS':<9} {'CREDIT':>14} {'SPENT':>14} {'REMAINING':>14}")
            for row in list_keys(db_path):
                print(
                    f"{row['name']:<20} {('enabled' if row['enabled'] else 'disabled'):<9} "
                    f"{format_credit(row, 'credit_microusd'):>14} {format_credit(row, 'spent_microusd'):>14} "
                    f"{(format_usd(remaining_credit(row)) if row['credit_enabled'] else 'not configured'):>14}"
                )
        elif args.command == "show":
            print_key(find_key(db_path, args.name))
        elif args.command == "set-credit":
            set_credit(db_path, args.name, args.credit_usd)
            print_key(find_key(db_path, args.name))
        elif args.command == "topup":
            if args.usd is None or args.usd <= 0:
                parser.error("--usd must be a positive amount")
            add_credit(db_path, args.name, args.usd)
            print_key(find_key(db_path, args.name))
        elif args.command == "set-quota":
            set_quota(db_path, args.name, args.quota_tokens)
            print_key(find_key(db_path, args.name))
        elif args.command == "add-tokens":
            add_quota(db_path, args.name, args.tokens)
            print_key(find_key(db_path, args.name))
        elif args.command in {"enable", "disable"}:
            set_enabled(db_path, args.name, args.command == "enable")
            print_key(find_key(db_path, args.name))
        elif args.command == "rotate":
            api_key = rotate_key(db_path, args.name)
            print("API key rotated. Save this new value now; the old key is invalid:")
            print(api_key)
        elif args.command == "logs":
            if args.limit < 1:
                parser.error("--limit must be positive")
            print(f"{'TIME':<27} {'STATUS':<18} {'MODEL':<22} {'IN':>8} {'OUT':>8} {'COST':>12} {'HTTP':>5}")
            for row in usage_logs(db_path, args.name, args.limit):
                print(
                    f"{row['occurred_at']:<27} {row['status']:<18} {(row['model_id'] or 'local-embedding'):<22} "
                    f"{row['input_tokens']:>8,} {row['output_tokens']:>8,} "
                    f"{format_usd(row['charged_microusd']):>12} {str(row['http_status'] or '-'):>5}"
                )
    except (KeyError, ValueError, OSError, sqlite3.IntegrityError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
