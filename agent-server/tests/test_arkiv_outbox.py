"""What reaches Arkiv, and that publishing can never affect a run."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.arkiv_outbox import ArkivOutbox, card_fields, receipt_fields
from app.main import create_app
from app.settings import Settings
from app.store import AgentStore
from app.templates import AVATARS, CATEGORIES, COLOURS

from .test_agent import AGENT, EXT, Backend, FakeAdmin, Results
from .test_payment import FakeSigner

SECRET = "SECRET-DETAIL"
HASH = "0x" + "b" * 64
FIELDS = {"name": "Butler", "description": SECRET, "avatar": "chef", "colour": "saffron", "instructions": SECRET, "profile": SECRET,
          "sites": ["secret.example"], "start_url": "https://secret.example/", "per_order_max_inr": 300, "category": "food", "published": True}


def make(post=lambda url, token, op: 200, url="http://publisher", token="t"):
    store = AgentStore(":memory:")
    agent = store.create_agent(FIELDS, "5")
    store.assign_wallet(agent["id"], "0x" + "a" * 40)
    return store, store.agent(agent["id"]), ArkivOutbox(store, url, token, "arc", post=post)


def finished_run(store, agent, status="PAID", rehearse=False, run_id="r1"):
    store.start_run(run_id, agent["id"], SECRET, rehearse, 30000)
    store.finish_run(run_id, status, SECRET, "secret.example", 23200, HASH, HASH)
    return store.run(run_id)


def test_nothing_private_can_leave():
    store, agent, outbox = make()
    outbox.publish_history(agent)
    outbox.run_started(agent)
    outbox.run_finished(agent, finished_run(store, agent))
    sent = []
    outbox.post = lambda url, token, op: sent.append(op) or 200
    while outbox.deliver_one() != "idle":
        pass
    assert {op["kind"] for op in sent} == {"card", "run_started", "run_finished"}
    assert SECRET not in json.dumps(sent) and "secret.example" not in json.dumps(sent)


def test_only_runs_that_locked_funds_get_a_receipt():
    store, agent, _ = make()
    assert receipt_fields(finished_run(store, agent), "arc")["outcome"] == "paid"
    assert receipt_fields(finished_run(store, agent, "NOT_PAID", run_id="r2"), "arc")["outcome"] == "not_paid"
    assert receipt_fields(finished_run(store, agent, "DRY_RUN", run_id="r3"), "arc") is None
    assert receipt_fields(finished_run(store, agent, rehearse=True, run_id="r4"), "arc") is None


def test_an_unpublished_agent_or_no_publisher_queues_nothing():
    store, agent, outbox = make()
    outbox.run_started({**agent, "published": False})
    assert store.outbox_next() is None
    store, agent, off = make(url="", token="")
    off.run_started(agent)
    off.card_changed(agent)
    assert store.outbox_next() is None


def test_a_failure_while_queueing_never_reaches_the_run():
    store, agent, outbox = make()
    store.outbox_add = None  # calling it raises
    outbox.run_started(agent)
    outbox.run_finished(agent, {"status": "PAID"})


def test_delivery_keeps_order_retries_and_drops_only_what_is_refused():
    answers = [503, 409, 400, 200]
    store, agent, outbox = make(post=lambda url, token, op: answers.pop(0))
    outbox.card_changed(agent)
    outbox.run_started(agent)
    first = store.outbox_next()["id"]
    assert outbox.deliver_one() == "retry" and store.outbox_next()["id"] == first
    assert outbox.deliver_one() == "retry" and store.outbox_next()["id"] == first
    assert outbox.deliver_one() == "dropped" and store.outbox_next()["id"] != first
    assert outbox.deliver_one() == "sent" and outbox.deliver_one() == "idle"


def test_a_publisher_that_cannot_be_reached_keeps_the_request():
    def down(url, token, op):
        raise OSError("refused")
    store, agent, outbox = make(post=down)
    outbox.card_changed(agent)
    assert outbox.deliver_one() == "retry" and store.outbox_next()


def test_an_older_database_keeps_its_agents_and_stays_private(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, avatar TEXT NOT NULL, colour TEXT NOT NULL, instructions TEXT NOT NULL, profile TEXT NOT NULL, sites TEXT NOT NULL, start_url TEXT NOT NULL, per_order_max_inr INTEGER NOT NULL, budget_usdc TEXT NOT NULL, wallet TEXT UNIQUE, wallet_status TEXT NOT NULL, wallet_detail TEXT NOT NULL, created REAL NOT NULL, archived REAL)")
    db.execute("INSERT INTO agents VALUES ('a','Old','','chef','teal','x','','[\"a.com\"]','https://a.com/',1,'1',NULL,'ready','',1.0,NULL)")
    db.commit()
    db.close()
    agent = AgentStore(str(path)).agent("a")
    assert agent["name"] == "Old" and agent["published"] is False and agent["category"] == "other"


def test_lists_match_the_publisher():
    text = (Path(__file__).resolve().parents[2] / "services" / "arkiv-board" / "src" / "ops.ts").read_text()
    for name, values in (("CATEGORIES", CATEGORIES), ("AVATARS", AVATARS), ("COLOURS", COLOURS)):
        found = re.search(rf"export const {name} = \[(.*?)\]", text).group(1)
        assert tuple(re.findall(r'"([^"]+)"', found)) == tuple(values)


def test_card_has_only_public_fields():
    assert set(card_fields({**FIELDS, "wallet": "0x1"})) == {"wallet", "category", "name", "avatar", "colour"}


def routes(record=lambda wallet: {"stats": {"paid": 2}, "expiresAt": "soon", "working": False}):
    store = AgentStore(":memory:")
    outbox = ArkivOutbox(store, "http://publisher", "t", "arc", post=lambda url, token, op: 200)
    backend = Backend()
    app = create_app(Settings(global_max_inr=30), client=backend, signer_for=lambda wallet: FakeSigner(wallet), results=Results(), store=store,
                     admin=FakeAdmin(backend), background=lambda work: work(), wallet_options={"poll_sec": 0, "wait_sec": 0, "sleep": lambda _: None},
                     outbox=outbox, public_record=record)
    client = TestClient(app)
    created = client.post("/agents", json=AGENT, headers=EXT)
    assert created.status_code == 201, created.text
    return client, store, created.json()["id"]


def kinds(store):
    kinds = []
    while (next_op := store.outbox_next()):
        kinds.append(json.loads(next_op["op"])["kind"])
        store.outbox_done(next_op["id"])
    return kinds


def test_a_new_agent_is_private_until_someone_publishes_it():
    client, store, agent_id = routes()
    agent = client.get(f"/agents/{agent_id}", headers=EXT).json()
    assert agent["published"] is False
    assert client.get(f"/agents/{agent_id}/public-record", headers=EXT).json() == {"published": False}
    client.patch(f"/agents/{agent_id}", json={"name": "Renamed"}, headers=EXT)
    assert kinds(store) == []


def test_the_switch_publishes_and_withdraws():
    client, store, agent_id = routes()
    assert client.patch(f"/agents/{agent_id}", json={"published": True, "category": "food"}, headers=EXT).status_code == 200
    assert kinds(store) == ["card"]
    client.patch(f"/agents/{agent_id}", json={"name": "Renamed"}, headers=EXT)
    assert kinds(store) == ["card"]
    client.patch(f"/agents/{agent_id}", json={"instructions": "Private change"}, headers=EXT)
    assert kinds(store) == []  # not public, so there's nothing to tell anyone
    client.patch(f"/agents/{agent_id}", json={"published": False}, headers=EXT)
    ops = store.outbox_next()
    assert json.loads(ops["op"])["kind"] == "withdraw" and json.loads(ops["op"])["receipts"] is True


def test_turning_it_on_publishes_earlier_receipts():
    client, store, agent_id = routes()
    finished_run(store, store.agent(agent_id))
    client.patch(f"/agents/{agent_id}", json={"published": True}, headers=EXT)
    assert kinds(store) == ["card", "run_finished"]


def test_archiving_keeps_receipts_but_removes_the_card():
    client, store, agent_id = routes()
    client.patch(f"/agents/{agent_id}", json={"published": True}, headers=EXT)
    kinds(store)
    assert client.delete(f"/agents/{agent_id}", headers=EXT).status_code == 200
    assert json.loads(store.outbox_next()["op"])["receipts"] is False


def test_an_unknown_category_is_refused():
    client, _, agent_id = routes()
    assert client.patch(f"/agents/{agent_id}", json={"category": "secret"}, headers=EXT).status_code == 422


def test_the_public_record_comes_from_arkiv_and_a_failure_says_so():
    client, _, agent_id = routes()
    client.patch(f"/agents/{agent_id}", json={"published": True}, headers=EXT)
    view = client.get(f"/agents/{agent_id}/public-record", headers=EXT).json()
    assert view["listed"] is True and view["stats"] == {"paid": 2}

    def down(wallet):
        raise OSError("no route")
    client, _, agent_id = routes(down)
    client.patch(f"/agents/{agent_id}", json={"published": True}, headers=EXT)
    assert client.get(f"/agents/{agent_id}/public-record", headers=EXT).json()["unavailable"] is True

    client, _, agent_id = routes(lambda wallet: None)
    client.patch(f"/agents/{agent_id}", json={"published": True}, headers=EXT)
    assert client.get(f"/agents/{agent_id}/public-record", headers=EXT).json()["listed"] is False


def test_the_run_path_cannot_reach_the_outbox():
    root = Path(__file__).resolve().parents[1] / "app"
    for path in [root / "machine.py", *(root / "payment").glob("*.py"), *(root / "deciders").glob("*.py")]:
        assert "arkiv" not in path.read_text().lower(), path
