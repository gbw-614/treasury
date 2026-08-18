"""Container-side reviewer account administration."""
from __future__ import annotations

import argparse
from getpass import getpass

from app.services import auth_store


def _new_password() -> str:
    first = getpass("Password: ")
    second = getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords did not match")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage label-verification reviewer accounts")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "reset-password"):
        command = commands.add_parser(name)
        command.add_argument("username")
    commands.add_parser("list")
    args = parser.parse_args()

    if args.command == "list":
        for user in auth_store.list_users():
            print(user.username)
        return

    try:
        user = (
            auth_store.create_user(args.username, _new_password())
            if args.command == "create"
            else auth_store.reset_password(args.username, _new_password())
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"{args.command} completed for {user.username}")


if __name__ == "__main__":
    main()
