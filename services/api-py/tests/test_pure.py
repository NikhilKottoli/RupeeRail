"""Quote maths, QR parser, commitment vector, EIP-712 signing/recovery, key loader."""

from __future__ import annotations

import pytest
from eth_account import Account
from eth_utils import keccak

from app.commitment import hex32, qr_commitment
from app.eip712 import (
    DISPUTE_INTENT_TYPESTRING, OFFRAMP_INTENT_TYPESTRING, SECP256K1_N, OfframpIntent, dispute_digest, domain_separator,
    dispute_typed_data, ecdsa_try_recover, intent_digest, intent_typed_data, normalize_v, permit_digest,
    permit_domain, permit_typed_data, sign_digest_typed, verify_eoa, vault_domain,
)
from app.keys import KeyLoadError, load_keys, load_user_key, read_env_file
from app.qr import parse_upi_qr
from app.quote_math import (
    offramp_amount, offramp_rate_e6, onramp_amount, onramp_rate_e6, price_to_e6, rupees_to_paise,
)

from .conftest import QUOTE_SIGNER_PK, USER_PK

# ---- quote maths ------------------------------------------------------------------------------


def test_offramp_vector():
    rate = offramp_rate_e6(price_to_e6("100.14"), 80)
    assert rate == 99338880
    assert offramp_amount(1000, 6, rate) == 100666  # Rs 10 -> ceil(1e13 / 99338880)


def test_offramp_rounds_up_and_exact_division_is_exact():
    assert offramp_amount(1, 6, 3_000_000) == 3334  # 3333.33 -> 3334
    assert offramp_amount(100, 6, 100_000_000) == 10_000  # exact: no extra unit


def test_onramp_floor():
    rate = onramp_rate_e6(price_to_e6("101.00"), 80)
    assert rate == 101808000
    assert onramp_amount(1000, 6, rate) == 98224  # floor(98224.1...)
    assert onramp_amount(1, 6, 3_000_000) == 3333  # 3333.33 -> 3333


def test_price_and_rupee_parsing():
    assert price_to_e6("100.1234567") == 100123456  # truncates past 6 dp
    assert price_to_e6("99") == 99_000_000
    assert rupees_to_paise("10") == 1000
    assert rupees_to_paise("10.5") == 1050
    assert rupees_to_paise("0.01") == 1
    for bad in ("10.005", "-1", "abc", ""):
        with pytest.raises(ValueError):
            rupees_to_paise(bad)


# ---- QR parser --------------------------------------------------------------------------------


def test_qr_static():
    p = parse_upi_qr("upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR", 1000)
    assert p.ok and p.upi_id == "chaipoint@okaxis" and p.payee_name == "Chai Point" and p.amount_paise is None


def test_qr_dynamic_match_and_mismatch():
    ok = parse_upi_qr("upi://pay?pa=shop@ybl&pn=Shop&am=10.00&cu=INR", 1000)
    assert ok.ok and ok.amount_paise == 1000
    assert parse_upi_qr("upi://pay?pa=shop@ybl&am=10.5", 1050).ok
    bad = parse_upi_qr("upi://pay?pa=shop@ybl&pn=Shop&am=10.00&cu=INR", 1100)
    assert not bad.ok and bad.reason == 2


def test_qr_plus_and_percent_decoding():
    assert parse_upi_qr("upi://pay?pa=a.b@okhdfc&pn=Chai+Point", 100).payee_name == "Chai Point"
    assert parse_upi_qr("upi://pay?pa=a.b@okhdfc&pn=Chai%20Point", 100).payee_name == "Chai Point"
    assert parse_upi_qr("UPI://PAY?PA=a.b@okhdfc", 100).upi_id == "a.b@okhdfc"  # case-insensitive scheme/keys


@pytest.mark.parametrize("qr,reason", [
    ("upi://pay?pa=x@okaxis", 1),            # VPA local part too short
    ("upi://pay?pa=bad vpa@okaxis", 1),
    ("upi://pay?pa=name@ok-axis", 1),         # handle must be alphanumeric
    ("upi://pay?pn=NoPa", 1),                 # missing pa
    ("000201010211" + "0" * 40, 1),           # EMV / BharatQR
    ("upi://pay?pa=shop@ybl&cu=USD", 1),      # non-INR
    ("upi://pay?pa=shop@ybl&pn=" + "A" * 520, 1),  # > 512 bytes
    ("", 1),
    ("https://example.com/pay?pa=shop@ybl", 1),
    ("upi://pay?pa=shop@ybl&am=ten", 1),
])
def test_qr_invalid(qr, reason):
    p = parse_upi_qr(qr, 1000)
    assert not p.ok and p.reason == reason


def test_qr_512_boundary():
    base = "upi://pay?pa=shop@ybl&pn="
    assert parse_upi_qr(base + "A" * (512 - len(base)), 100).ok
    assert not parse_upi_qr(base + "A" * (513 - len(base)), 100).ok


# ---- commitment -------------------------------------------------------------------------------


def test_commitment_vector():
    c = qr_commitment("0xf32a38ae2d57bff16f79655840ea3a427cce784427e9213ce8d2488b2460cf47",
                      "upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR")
    assert hex32(c) == "0x1f927e720f477505fed7b6d38698acf7a2f61bf925ddff1c98344d769dcfeb8a"


def test_commitment_is_salted():
    qr = "upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR"
    assert qr_commitment(b"\x01" * 32, qr) != qr_commitment(b"\x02" * 32, qr)


# ---- EIP-712 ----------------------------------------------------------------------------------

DOM = vault_domain(5042002, "0x878734374e6131fceeeb83fa8605ab49f4d21836")


def _intent(user: str) -> OfframpIntent:
    return OfframpIntent(id="0x" + "11" * 32, user=user, amount=100666, inr_paise=1000, rate_e6=99338880,
                         qr_commitment="0x1f927e720f477505fed7b6d38698acf7a2f61bf925ddff1c98344d769dcfeb8a",
                         deadline=1_800_000_000)


def test_typehashes_match_contract_strings():
    assert keccak(text=OFFRAMP_INTENT_TYPESTRING) == keccak(
        text="OfframpIntent(bytes32 id,address user,uint256 amount,uint64 inrPaise,uint64 rateE6,bytes32 qrCommitment,uint64 deadline)")
    assert DISPUTE_INTENT_TYPESTRING == "DisputeIntent(bytes32 id,address user,uint64 deadline)"


def test_quote_signer_signature_recovers_to_configured_address():
    signer = Account.from_key(QUOTE_SIGNER_PK)
    intent = _intent(Account.from_key(USER_PK).address)
    sig = sign_digest_typed(signer, intent_typed_data(DOM, intent))
    assert len(sig) == 65 and sig[64] in (27, 28)
    assert ecdsa_try_recover(intent_digest(DOM, intent), sig) == signer.address
    # any field change breaks it
    other = OfframpIntent(**{**intent.__dict__, "inr_paise": 1001})
    assert ecdsa_try_recover(intent_digest(DOM, other), sig) != signer.address


def test_manual_digest_matches_eth_account():
    from eth_abi import encode
    intent = _intent(Account.from_key(USER_PK).address)
    struct = keccak(encode(
        ["bytes32", "bytes32", "address", "uint256", "uint64", "uint64", "bytes32", "uint64"],
        [keccak(text=OFFRAMP_INTENT_TYPESTRING), *intent.as_tuple()]))
    assert keccak(b"\x19\x01" + domain_separator(DOM) + struct) == intent_digest(DOM, intent)


def test_dispute_and_permit_sign_recover():
    u = Account.from_key(USER_PK)
    d = dispute_digest(DOM, "0x" + "22" * 32, u.address, 1_800_000_000)
    sig = sign_digest_typed(u, dispute_typed_data(DOM, "0x" + "22" * 32, u.address, 1_800_000_000))
    assert verify_eoa(d, sig, u.address)
    pd = permit_domain("USDC", "2", 5042002, "0x3600000000000000000000000000000000000000")
    psig = sign_digest_typed(u, permit_typed_data(pd, u.address, DOM["verifyingContract"], 5, 0, 1_800_000_000))
    assert verify_eoa(permit_digest(pd, u.address, DOM["verifyingContract"], 5, 0, 1_800_000_000), psig, u.address)
    assert not verify_eoa(permit_digest(pd, u.address, DOM["verifyingContract"], 5, 1, 1_800_000_000), psig, u.address)


def test_signature_rules_mirror_oz_ecdsa():
    u = Account.from_key(USER_PK)
    intent = _intent(u.address)
    digest = intent_digest(DOM, intent)
    sig = sign_digest_typed(u, intent_typed_data(DOM, intent))
    v01 = sig[:64] + bytes([sig[64] - 27])
    assert ecdsa_try_recover(digest, v01) is None  # ecrecover needs 27/28
    assert ecdsa_try_recover(digest, normalize_v(v01)) == u.address
    s = int.from_bytes(sig[32:64], "big")
    high = sig[:32] + (SECP256K1_N - s).to_bytes(32, "big") + bytes([55 - sig[64]])
    assert ecdsa_try_recover(digest, high) is None  # malleable high-s rejected, like OZ
    assert ecdsa_try_recover(digest, sig[:64]) is None  # 64-byte rejected (OZ 5.1 accepts 65 only)


# ---- keys -------------------------------------------------------------------------------------

SECRET_PHRASE = "test test test test test test test test test test test junk"


def test_key_loader_reads_env_file_as_text(tmp_path, monkeypatch):
    for v in ("SEED_PHRASE", "QUOTE_SIGNER_PRIVATE_KEY", "PAYMASTER_PRIVATE_KEY", "SETTLER_PRIVATE_KEY",
              "OTHER_WALLET_PRIVATE_KEY"):
        monkeypatch.delenv(v, raising=False)
    env = tmp_path / ".env"
    env.write_text(f'SEED_PHRASE="{SECRET_PHRASE}"\nOTHER_WALLET_PRIVATE_KEY={USER_PK}\n')
    k = load_keys(env)
    assert k.quote_signer.address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # HD index 0
    assert SECRET_PHRASE not in repr(k)
    assert load_user_key(env).address == Account.from_key(USER_PK).address
    monkeypatch.setenv("PAYMASTER_PRIVATE_KEY", OPERATOR_OVERRIDE)
    assert load_keys(env).paymaster.address == Account.from_key(OPERATOR_OVERRIDE).address
    assert read_env_file(env, "MISSING") is None


OPERATOR_OVERRIDE = "0x" + "4" * 64


def test_key_errors_never_contain_the_value(tmp_path, monkeypatch):
    for v in ("SEED_PHRASE", "QUOTE_SIGNER_PRIVATE_KEY", "PAYMASTER_PRIVATE_KEY", "SETTLER_PRIVATE_KEY"):
        monkeypatch.delenv(v, raising=False)
    bad_phrase = "zebra " * 11 + "notaword"
    env = tmp_path / ".env"
    env.write_text(f"SEED_PHRASE={bad_phrase}\n")
    with pytest.raises(KeyLoadError) as e:
        load_keys(env)
    assert "zebra" not in str(e.value) and "notaword" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
    monkeypatch.setenv("QUOTE_SIGNER_PRIVATE_KEY", "0xdeadbeefnothex")
    with pytest.raises(KeyLoadError) as e2:
        load_keys(env)
    assert "deadbeef" not in str(e2.value)
