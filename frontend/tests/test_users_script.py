"""deploy/lambda/users.py against a fake Secrets Manager client."""

import importlib.util
import json
from pathlib import Path

import pytest

from passwords import verify_password

_spec = importlib.util.spec_from_file_location("users_script", Path(__file__).resolve().parents[1] / "deploy" / "lambda" / "users.py")
users_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(users_script)


class FakeSecrets:
    def __init__(self, value="legacy-shared-password"):
        self.value = value

    def get_secret_value(self, SecretId):
        return {"SecretString": self.value}

    def put_secret_value(self, SecretId, SecretString):
        self.value = SecretString


def _password(output: str) -> str:
    return next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("password: "))


def test_add_stores_only_a_hash_and_shows_the_password_once():
    sm = FakeSecrets()
    out = users_script.run(sm, "s", "add", "alice")
    stored = json.loads(sm.value)["users"]
    assert list(stored) == ["alice"] and _password(out) not in sm.value
    assert verify_password(_password(out), stored["alice"])


def test_reset_remove_and_list():
    sm = FakeSecrets()
    first = _password(users_script.run(sm, "s", "add", "alice"))
    users_script.run(sm, "s", "add", "bob")
    second = _password(users_script.run(sm, "s", "reset", "alice"))
    assert not verify_password(first, json.loads(sm.value)["users"]["alice"])
    assert verify_password(second, json.loads(sm.value)["users"]["alice"])
    users_script.run(sm, "s", "remove", "bob")
    assert users_script.run(sm, "s", "list", None) == "alice"


@pytest.mark.parametrize("command, user", [("add", "alice"), ("reset", "nobody"), ("remove", "nobody"), ("add", "bad name!")])
def test_refuses_what_makes_no_sense(command, user):
    sm = FakeSecrets(json.dumps({"users": {"alice": "x"}}))
    with pytest.raises(SystemExit):
        users_script.run(sm, "s", command, user)
