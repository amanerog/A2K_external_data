"""Users of the frontend page: one user and password per person, kept in the
stack's Secrets Manager secret as {"users": {"<name>": "<pbkdf2 hash>"}}.
The page reloads the list every few minutes (A2K_FRONTEND_USERS_TTL, 300 s),
so a change needs no redeploy.

    python deploy/lambda/users.py add alice      # prints alice's password, once
    python deploy/lambda/users.py reset alice    # a new password for alice
    python deploy/lambda/users.py remove alice
    python deploy/lambda/users.py list

Only a hash is stored: a lost password is replaced with `reset`, never read
back. The first `add` replaces the stack's initial single shared password
(user "a2k"), which then stops working.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "grc"))
from passwords import hash_password, new_password  # noqa: E402

USER_NAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def load(client, secret_id: str) -> dict[str, str]:
    raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        return dict(json.loads(raw)["users"])
    except (ValueError, KeyError, TypeError):
        return {}  # still the stack's single plain password


def save(client, secret_id: str, users: dict[str, str]) -> None:
    client.put_secret_value(SecretId=secret_id, SecretString=json.dumps({"users": users}, sort_keys=True))


def run(client, secret_id: str, command: str, user: str | None) -> str:
    """Applies `command`; returns what to print."""
    users = load(client, secret_id)
    if command == "list":
        return "\n".join(sorted(users)) or "(no users -- the page still uses the stack's single password, user a2k)"
    if not user or not USER_NAME.match(user):
        raise SystemExit("a user name: letters, digits and . _ @ - (e.g. an e-mail or x637800)")
    if command == "remove":
        if user not in users:
            raise SystemExit(f"{user} is not a user")
        del users[user]
        save(client, secret_id, users)
        return f"{user} removed (effective within the page's reload interval, 5 min by default)"
    if command == "add" and user in users:
        raise SystemExit(f"{user} already exists -- use reset for a new password")
    if command == "reset" and user not in users:
        raise SystemExit(f"{user} is not a user -- use add")
    password = new_password()
    users[user] = hash_password(password)
    save(client, secret_id, users)
    return f"user: {user}\npassword: {password}\n(shown only now -- hand it to {user} privately)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("add", "reset", "remove", "list"))
    parser.add_argument("user", nargs="?")
    parser.add_argument("--secret-id", default="a2k-frontend/password")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    args = parser.parse_args()
    print(run(boto3.client("secretsmanager", region_name=args.region), args.secret_id, args.command, args.user))


if __name__ == "__main__":
    main()
