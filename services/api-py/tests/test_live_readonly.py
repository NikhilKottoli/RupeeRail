"""Live, READ-ONLY checks against both testnets (eth_call / eth_getLogs / eth_chainId only).

Deselect offline with:  uv run pytest -m "not live"
"""

from __future__ import annotations

import time

import pytest
from eth_account import Account

from app.chain import ChainClient, ChainError
from app.commitment import hex32, qr_commitment, random_bytes32
from app.config import Settings, load_chain
from app.eip712 import (
    OfframpIntent, dispute_digest, domain_separator, intent_digest, intent_typed_data, permit_domain,
    permit_typed_data, sign_digest_typed, split_sig, vault_domain,
)
from app.keys import KeyLoadError, load_keys

from .conftest import QR, USER_PK

pytestmark = pytest.mark.live

CHAINS = ["arc", "whitechain"]


@pytest.fixture(params=CHAINS)
async def live(request):
    cfg = load_chain(request.param)
    c = ChainClient(cfg, dry_run=True)
    yield cfg, c
    await c.close()


def _intent(user: str) -> OfframpIntent:
    return OfframpIntent(id=hex32(random_bytes32()), user=user, amount=100666, inr_paise=1000, rate_e6=99338880,
                         qr_commitment=hex32(qr_commitment(random_bytes32(), QR)), deadline=int(time.time()) + 90)


async def test_chain_id(live):
    cfg, c = live
    assert await c.chain_id() == cfg.chain_id


async def test_intent_digest_matches_vault_hashIntent(live):
    cfg, c = live
    intent = _intent(Account.from_key(USER_PK).address)
    ours = intent_digest(vault_domain(cfg.chain_id, cfg.vault), intent)
    assert ours == await c.hash_intent(intent.as_tuple())


async def test_dispute_digest_matches_vault_hashDispute(live):
    cfg, c = live
    id_, user, deadline = hex32(random_bytes32()), Account.from_key(USER_PK).address, int(time.time()) + 600
    assert dispute_digest(vault_domain(cfg.chain_id, cfg.vault), id_, user, deadline) == \
        await c.hash_dispute(id_, user, deadline)


async def test_permit_domain_separator_matches_token(live):
    cfg, c = live
    pd = permit_domain(cfg.token.permit_name, cfg.token.permit_version, cfg.chain_id, cfg.token.address)
    assert domain_separator(pd) == await c.token_domain_separator()


async def test_vault_domain_separator_via_eip712Domain(live):
    cfg, c = live
    fields, name, version, chain_id, contract, salt, ext = await c.call("eip712Domain")
    assert (name, version, chain_id, contract.lower()) == ("ScanPayVault", "1", cfg.chain_id, cfg.vault.lower())


async def test_listener_reads_and_decodes_logs(live):
    cfg, c = live
    start = await c.start_block()
    events = await c.get_logs(start, start + 1999)
    assert any(e.name == "OfframpLocked" for e in events)
    lock = next(e for e in events if e.name == "OfframpLocked")
    st = await c.offramp_state(lock.args["id"])
    assert st.qr_commitment == lock.args["qrCommitment"] and st.amount == lock.args["amount"]


async def test_revert_decoding(live):
    cfg, c = live
    with pytest.raises(ChainError) as e:
        await c.simulate("0x000000000000000000000000000000000000dEaD", "completeOfframp", [b"\x01" * 32, b"\x02" * 32])
    assert e.value.name == "UnauthorizedSettler"
    with pytest.raises(ChainError) as e:
        await c.simulate("0x000000000000000000000000000000000000dEaD", "releaseOfframp", [b"\x01" * 32])
    assert e.value.name == "InvalidStatus" and e.value.err_args["actual"] == "NONE"


async def test_full_offramp_simulation_passes_signature_checks(live):
    """eth_call of offramp() with OUR quote signature and a throwaway user's signature + permit.

    The throwaway user has no tokens, so the call must pass the user-signature check, the
    quote-signature check and the permit (allowance check passes, proving the permit signature
    is valid for the token), and stop only at the token pull with "transfer amount exceeds
    balance". It never broadcasts. Skipped if the configured quote signer key is unavailable.
    """
    cfg, c = live
    try:
        keys = load_keys(Settings().env_file)
    except KeyLoadError:
        pytest.skip("quote signer key not available")
    if (await c.call("quoteSigner")).lower() != keys.quote_signer.address.lower():
        pytest.fail("configured quote signer is not the vault's quoteSigner")
    user = Account.from_key(USER_PK)  # throwaway, zero balance
    vd = vault_domain(cfg.chain_id, cfg.vault)
    intent = _intent(user.address)
    usig = sign_digest_typed(user, intent_typed_data(vd, intent))
    qsig = sign_digest_typed(keys.quote_signer, intent_typed_data(vd, intent))
    pd = permit_domain(cfg.token.permit_name, cfg.token.permit_version, cfg.chain_id, cfg.token.address)
    nonce = await c.token_nonce(user.address)
    psig = sign_digest_typed(user, permit_typed_data(pd, user.address, cfg.vault, intent.amount, nonce, intent.deadline))
    v, r, s = split_sig(psig)
    args = [intent.as_tuple(), usig, qsig, (intent.deadline, v, r, s)]
    with pytest.raises(ChainError) as e:
        await c.simulate(keys.paymaster.address, "offramp", args)
    print(f"\n[{cfg.key}] offramp simulation stopped at: {e.value}")
    assert e.value.name == "Error" and "exceeds balance" in e.value.err_args["message"], str(e.value)
    # a wrong quote signer is caught by the vault
    bad = sign_digest_typed(Account.create(), intent_typed_data(vd, intent))
    with pytest.raises(ChainError) as e2:
        await c.simulate(keys.paymaster.address, "offramp", [intent.as_tuple(), usig, bad, (intent.deadline, v, r, s)])
    assert e2.value.name == "InvalidQuote"
