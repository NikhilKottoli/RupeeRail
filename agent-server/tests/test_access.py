"""A hosted server: invite codes on every route and socket, and payer results over the network.

Starlette's websocket_connect writes the subprotocol into the headers it's given, so each call gets its own copy of EXT.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.payment.results import LedgerResults, ReportedResults
from app.settings import Settings, parse_access_codes
from app.store import AgentStore

from .test_agent import AGENT, EXT, Backend, FakeAdmin, new_agent, server

CODE = "invite-code-for-the-tests-1"
SECOND = "invite-code-for-the-tests-2"
TOKEN = "payer-token"
KEY = "0x" + "ab" * 32
WITH_CODE = {**EXT, "authorization": f"Bearer {CODE}"}
PROXIED = {"x-forwarded-for": "203.0.113.9"}
SOCKET_CODE = f"rupeerail.code.{CODE}"


def hosted(**over):
    return server(access_codes=(CODE, SECOND), **over)[0]


def run_on(client):
    agent = client.post("/agents", json=AGENT, headers=WITH_CODE).json()
    response = client.post(f"/agents/{agent['id']}/runs", json={"prompt": "tea", "rehearse": True}, headers=WITH_CODE)
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


@pytest.mark.parametrize("method, path", [
    ("GET", "/templates"), ("GET", "/agents"), ("POST", "/agents"), ("GET", "/agents/x"), ("PATCH", "/agents/x"), ("DELETE", "/agents/x"),
    ("POST", "/agents/x/budget"), ("POST", "/agents/x/fund"), ("GET", "/agents/x/runs"), ("POST", "/agents/x/runs"), ("GET", "/agents/x/public-record"),
    ("GET", "/runs/x"), ("POST", "/runs/x/stop"), ("GET", "/webhooks/test"), ("POST", "/webhooks/test"), ("GET", "/no/such/route"),
])
@pytest.mark.parametrize("headers", [{}, EXT, {**EXT, "authorization": "Bearer wrong"}, {**EXT, "authorization": CODE}, {"authorization": ""}], ids=["none", "extension", "wrong", "no-scheme", "empty"])
def test_every_route_needs_a_code(method, path, headers):
    assert hosted().request(method, path, headers=headers).status_code == 401


def test_a_forged_extension_origin_is_not_enough():
    assert hosted().get("/agents", headers={"origin": "chrome-extension://anything"}).status_code == 401


@pytest.mark.parametrize("code", [CODE, SECOND])
def test_each_code_gets_in(code):
    assert hosted().get("/agents", headers={**EXT, "authorization": f"Bearer {code}"}).status_code == 200


def test_a_website_is_still_refused_with_a_code():
    assert hosted().get("/agents", headers={"origin": "https://evil.example", "authorization": f"Bearer {CODE}"}).status_code == 403


def test_a_preflight_needs_no_code_and_leads_nowhere():
    response = hosted().options("/agents", headers={**EXT, "access-control-request-method": "GET", "access-control-request-headers": "authorization"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == EXT["origin"]


def test_a_proxied_request_is_refused_when_no_codes_are_set():
    """Forgetting AGENT_ACCESS_CODES on the VM must not open the server to the internet."""
    client, _ = server()
    assert client.get("/agents", headers=PROXIED).status_code == 401
    assert client.get("/agents", headers={**EXT, **PROXIED}).status_code == 401
    assert client.get("/agents", headers=EXT).status_code == 200  # on this computer, nothing changes


def test_open_access_lets_proxied_requests_in_only_when_asked():
    client, _ = server(open_access=True)
    assert client.get("/agents", headers={**EXT, **PROXIED}).status_code == 200
    assert client.get("/agents", headers={"origin": "https://evil.example", **PROXIED}).status_code == 403  # websites stay out


def test_codes_still_apply_if_open_access_is_also_set():
    assert server(access_codes=(CODE,), open_access=True)[0].get("/agents", headers={**EXT, **PROXIED}).status_code == 401


def test_a_proxied_socket_is_refused_when_no_codes_are_set():
    client, _ = server()
    agent = new_agent(client)
    rid = client.post(f"/agents/{agent['id']}/runs", json={"prompt": "tea", "rehearse": True}, headers=EXT).json()["run_id"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/runs/{rid}", headers={**EXT, **PROXIED}):
            pass


@pytest.mark.parametrize("subprotocols", [[], ["rupeerail.code.wrong"], [CODE]], ids=["none", "wrong", "no-prefix"])
def test_a_run_socket_needs_a_code(subprotocols):
    client = hosted()
    rid = run_on(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/runs/{rid}", headers={**EXT}, subprotocols=subprotocols):
            pass


def test_a_run_socket_opens_with_a_code_and_echoes_it():
    client = hosted()
    rid = run_on(client)
    with client.websocket_connect(f"/runs/{rid}", headers={**EXT}, subprotocols=[SOCKET_CODE]) as ws:
        assert ws.accepted_subprotocol == SOCKET_CODE  # a browser drops the socket otherwise


def test_a_local_socket_still_opens_without_one():
    client, _ = server()
    agent = new_agent(client)
    rid = client.post(f"/agents/{agent['id']}/runs", json={"prompt": "tea", "rehearse": True}, headers=EXT).json()["run_id"]
    with client.websocket_connect(f"/runs/{rid}", headers=EXT) as ws:
        assert ws.accepted_subprotocol is None


# ---- codes in the settings --------------------------------------------------------------------------


def test_codes_are_read_from_a_list():
    assert parse_access_codes(f" {CODE}, {SECOND} ,") == (CODE, SECOND)
    assert parse_access_codes("") == ()


@pytest.mark.parametrize("value", ["short", "password", f"{CODE},short", "has spaces in it which are not allowed", "x" * 23, "bad/characters/that-a-subprotocol-cant-carry"])
def test_a_weak_or_malformed_code_stops_the_server_starting(value):
    with pytest.raises(ValueError):
        parse_access_codes(value)


def test_settings_load_reads_both_secrets(monkeypatch):
    monkeypatch.setenv("AGENT_ACCESS_CODES", CODE)
    monkeypatch.setenv("PAYER_RESULTS_TOKEN", TOKEN)
    settings = Settings.load()
    assert settings.access_codes == (CODE,)
    assert settings.payer_results_token == TOKEN


# ---- payer results --------------------------------------------------------------------------------


def app_reading_results(store, **over):
    backend = Backend()
    settings = Settings(access_codes=(CODE,), **over)
    return create_app(settings, client=backend, store=store, admin=FakeAdmin(backend), outbox=None)


def test_the_server_reads_reports_once_it_has_a_token():
    assert isinstance(app_reading_results(AgentStore(":memory:"), payer_results_token=TOKEN).state.results, ReportedResults)
    assert isinstance(app_reading_results(AgentStore(":memory:")).state.results, LedgerResults)


def test_a_report_becomes_the_result_a_run_reads():
    app = app_reading_results(AgentStore(":memory:"), payer_results_token=TOKEN)
    client = TestClient(app)
    response = client.post("/payer-results", json={"key": KEY, "status": "paid"}, headers={"authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 202
    assert app.state.results.result(KEY.upper().replace("0X", "0x")) == "paid"
    assert app.state.results.result("0x" + "cd" * 32) is None


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", f"Bearer {CODE}"], ids=["none", "wrong", "invite-code"])
def test_a_guest_cant_claim_a_payment_was_made(authorization):
    client = hosted(payer_results_token=TOKEN)
    headers = {"authorization": authorization} if authorization else {}
    assert client.post("/payer-results", json={"key": KEY, "status": "paid"}, headers=headers).status_code == 401


def test_the_payer_token_doesnt_work_as_an_invite_code():
    client = hosted(payer_results_token=TOKEN)
    assert client.get("/agents", headers={**EXT, "authorization": f"Bearer {TOKEN}"}).status_code == 401


def test_reports_are_refused_where_the_payer_has_a_ledger():
    client = hosted()
    assert client.post("/payer-results", json={"key": KEY, "status": "paid"}, headers={"authorization": f"Bearer {TOKEN}"}).status_code == 404


@pytest.mark.parametrize("body", [{"key": "nope", "status": "paid"}, {"key": KEY, "status": "maybe"}, {"key": KEY}])
def test_a_malformed_report_is_refused(body):
    client = hosted(payer_results_token=TOKEN)
    assert client.post("/payer-results", json=body, headers={"authorization": f"Bearer {TOKEN}"}).status_code == 422


def test_a_report_can_change_when_an_unclear_payment_settles():
    store = AgentStore(":memory:")
    store.save_payer_result(KEY, "unknown")
    store.save_payer_result(KEY, "paid")
    assert store.payer_result(KEY) == "paid"


# ---- two servers on one Circle account ------------------------------------------------------------


def test_a_reserved_wallet_is_never_given_to_a_new_agent():
    in_use, spare = "0x" + "aa" * 20, "0x" + "bb" * 20
    backend = Backend()
    admin = FakeAdmin(backend, wallets=(in_use.upper().replace("0X", "0x"), spare))
    client = TestClient(create_app(Settings(global_max_inr=30, reserved_wallets=(in_use,)), client=backend, signer_for=lambda wallet: None, store=AgentStore(":memory:"), admin=admin,
                                   background=lambda work: work(), wallet_options={"poll_sec": 0, "wait_sec": 0, "sleep": lambda _: None}))
    first = new_agent(client)
    assert first["wallet"]["address"] == spare
    second = new_agent(client, name="Second")
    assert second["wallet"]["address"].lower() not in (in_use, spare)  # it makes a new wallet rather than touch one in use
    assert ("create", second["id"]) in admin.calls
    assert not any(call[0] == "approve" and call[1].lower() == in_use for call in admin.calls)


def test_reserved_wallets_are_read_from_a_list(monkeypatch):
    monkeypatch.setenv("AGENT_RESERVED_WALLETS", " 0xAA , 0xBB,")
    assert Settings.load().reserved_wallets == ("0xAA", "0xBB")
