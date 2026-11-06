"""APP_ENV: dev keeps today's behaviour exactly; prod refuses to start until its Razorpay rails are real."""

from __future__ import annotations

import shlex
import sys

import pytest
from fastapi.testclient import TestClient

from app import environment as envmod
from app.config import SERVICE_DIR, Settings
from app.context import build_context
from app.main import create_app
from app.payout_runner import run_black_box

from .conftest import ARC

ENV_VARS = ("APP_ENV", "PAYOUT_RAIL", "COLLECT_RAIL", "PAYOUT_COMMAND", "COLLECT_UPI_ID", "ADMIN_TOKEN",
            "VIEW_TOKEN_SECRET", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAYX_ACCOUNT_NUMBER",
            "RAZORPAY_WEBHOOK_SECRET")
ORIGINAL_STUB = f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVICE_DIR / 'scripts' / 'dev_payout_stub.py'))}"
READY = {"COLLECT_UPI_ID": "real@bank", "ADMIN_TOKEN": "t", "VIEW_TOKEN_SECRET": "v", "RAZORPAY_KEY_ID": "k",
         "RAZORPAY_KEY_SECRET": "s", "RAZORPAYX_ACCOUNT_NUMBER": "1", "RAZORPAY_WEBHOOK_SECRET": "w"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def use(monkeypatch, **env) -> Settings:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


# ---- dev: unchanged ----------------------------------------------------------------------------


@pytest.mark.parametrize("env", [{}, {"APP_ENV": "dev"}, {"APP_ENV": "dev", "PAYOUT_RAIL": "phone"}])
def test_dev_runs_the_same_stub_as_before(monkeypatch, env):
    s = use(monkeypatch, **env)
    assert s.payout_command == ORIGINAL_STUB
    assert envmod.problems(s) == []


def test_explicit_payout_command_still_wins(monkeypatch):
    assert use(monkeypatch, PAYOUT_COMMAND="/bin/true").payout_command == "/bin/true"
    assert use(monkeypatch, APP_ENV="prod", PAYOUT_COMMAND="/bin/true").payout_command == "/bin/true"


@pytest.mark.parametrize("var,value", [("APP_ENV", "staging"), ("PAYOUT_RAIL", "paytm"), ("COLLECT_RAIL", "cash")])
def test_unknown_names_are_rejected(monkeypatch, var, value):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=var):
        envmod.problems(Settings())


# ---- prod: locked until the Razorpay rails exist -----------------------------------------------


def test_prod_runs_the_razorpayx_rail(monkeypatch):
    assert use(monkeypatch, APP_ENV="prod").payout_command.endswith("razorpayx_payout.py")


def test_prod_lists_everything_missing(monkeypatch):
    found = envmod.problems(use(monkeypatch, APP_ENV="prod"))
    for expected in ("PAYOUT_RAIL=razorpayx is declared but not implemented yet",
                     "COLLECT_RAIL=razorpay is declared but not implemented yet",
                     "RAZORPAY_KEY_ID is not set", "RAZORPAY_WEBHOOK_SECRET is not set",
                     "COLLECT_UPI_ID is the placeholder rupeerail@okaxis",
                     "ADMIN_TOKEN is not set", "VIEW_TOKEN_SECRET is not set"):
        assert expected in found


def test_prod_fully_configured_is_blocked_only_by_the_unimplemented_rails(monkeypatch):
    assert envmod.problems(use(monkeypatch, APP_ENV="prod", **READY)) == [
        "COLLECT_RAIL=razorpay is declared but not implemented yet",
        "PAYOUT_RAIL=razorpayx is declared but not implemented yet"]


def test_prod_refuses_dev_rails_and_the_stub(monkeypatch):
    found = envmod.problems(use(monkeypatch, APP_ENV="prod", PAYOUT_RAIL="phone", COLLECT_RAIL="manual"))
    assert "PAYOUT_RAIL=phone is a dev rail" in found and "COLLECT_RAIL=manual is a dev rail" in found
    assert "PAYOUT_COMMAND runs the dev stub, which pays nobody" in found


def test_prod_server_does_not_start(monkeypatch, store, fx, keys, chain):
    s = use(monkeypatch, APP_ENV="prod")
    s.db_path, s.workers = ":memory:", False
    ctx = build_context(s, chains={"arc": ARC}, clients={"arc": chain}, store=store, fx=fx, keys=keys)
    with pytest.raises(RuntimeError, match="APP_ENV=prod is not ready"):
        with TestClient(create_app(ctx, workers=False, fx=False)):
            pass


async def test_razorpayx_placeholder_pays_nobody_and_fails_the_handoff(monkeypatch):
    ok, detail, stdout = await run_black_box(use(monkeypatch, APP_ENV="prod").payout_command,
                                             {"idempotencyKey": "0x01"}, 10)
    assert not ok and detail.startswith("exit 1") and "not implemented" in detail and stdout == ""
