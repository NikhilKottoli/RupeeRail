"""The payment path: checkout page -> QR -> quote -> verify -> sign -> submit -> poll.

It takes an Observation, the same shape the browser extension will send, so a test page and a real site go through
exactly the same code. The flow stops at the first failed check, before anything is signed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..signers.base import Signer
from .qr import QrError, find_payment, find_total
from .verify import Constraints, VerificationError, check_page, check_quote

FINAL_OK = {"COMPLETED", "RELEASED"}
FINAL_BAD = {"REFUNDED", "REVERSED", "DISPUTED"}
# A deliberately invalid permit. The backend accepts it when the allowance covers the payment, and the vault's
# try/catch ignores it, so the allowance stays as the wallet's spending cap.
NO_PERMIT = {"v": 27, "r": "0x" + "00" * 32, "s": "0x" + "00" * 32}


@dataclass
class Observation:
    url: str
    page_text: str
    screenshot: bytes | None = None
    known_total: int | None = None  # the total from an earlier page, for QR pages that don't repeat it


@dataclass
class PaymentResult:
    status: str
    offramp_id: str | None = None
    tx_hash: str | None = None
    amount_paise: int | None = None
    payee: str | None = None
    token_amount: str | None = None
    detail: str = ""
    stages: list[str] = field(default_factory=list)


class PaymentAborted(Exception):
    pass


class PaymentFlow:
    def __init__(self, client: Any, signer: Signer, chain: str, constraints: Constraints,
                 emit: Callable[[str], None] = lambda _: None, should_stop: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 poll_sec: float = 3.0, poll_timeout_sec: float = 120.0) -> None:
        self.client, self.signer, self.chain, self.constraints = client, signer, chain, constraints
        self.emit, self.should_stop, self.clock, self.sleep = emit, should_stop, clock, sleep
        self.poll_sec, self.poll_timeout_sec = poll_sec, poll_timeout_sec
        self.submitted = False  # one payment per session, even if a later step fails
        self.offramp_id: str | None = None  # known once submitted, even if the submit call itself fails

    def _checkpoint(self) -> None:
        if self.should_stop():
            raise PaymentAborted("stopped by the user")

    def _preflight(self, chain_info: dict, needed: int | None) -> None:
        account = self.client.account(self.chain, self.signer.address())
        if needed is not None and int(account["balance"]) < needed:
            raise VerificationError([f"wallet balance {account['balance']} is below the {needed} needed"])
        if needed is not None and self.signer.uses_allowance and int(account["allowance"]) < needed:
            raise VerificationError([f"vault allowance {account['allowance']} is below the {needed} needed"])

    def pay(self, observation: Observation) -> PaymentResult:
        if self.submitted:
            return PaymentResult("REFUSED", detail="this session has already made its one payment")
        stages: list[str] = []

        def step(message: str) -> None:
            stages.append(message)
            self.emit(message)

        try:
            self._checkpoint()
            step("reading the QR")
            payment = find_payment(observation.page_text, observation.screenshot)
            total = find_total(observation.page_text)
            if total is None:
                total = observation.known_total if observation.known_total is not None else payment.amount_paise
            step(f"page asks for {total} paise, QR pays {payment.upi_id}")
            problems = check_page(payment, total, observation.url, self.constraints)
            if problems:
                raise VerificationError(problems)

            config = self.client.config()
            chain_info = config["chains"][self.chain]
            self._preflight(chain_info, None)
            self._checkpoint()

            # The quote lasts 90 seconds, so it's requested only now that everything else has passed.
            step("requesting a quote")
            inr = f"{total // 100}.{total % 100:02d}"
            quote = self.client.quote(self.chain, self.signer.address(), inr, payment.qr_text)
            step("verifying the quote")
            problems = check_quote(quote, payment=payment, page_total=total, wallet=self.signer.address(),
                                   chain_info=chain_info, constraints=self.constraints, now=self.clock())
            if problems:
                raise VerificationError(problems)
            self._preflight(chain_info, int(quote["intent"]["amount"]))
            self._checkpoint()

            step("signing")
            permit = quote["signing"]["permit"]
            user_sig = self.signer.sign_typed_data(quote["signing"]["intent"])
            if self.signer.uses_allowance:
                permit_fields = dict(NO_PERMIT)
            else:
                signature = self.signer.sign_typed_data(permit)
                v = int(signature[130:132], 16)
                permit_fields = {"v": v + 27 if v < 27 else v, "r": "0x" + signature[2:66], "s": "0x" + signature[66:130]}
            permit_fields["deadline"] = str(permit["message"]["deadline"])
            self._checkpoint()  # the last moment a kill switch can stop the payment

            step("submitting")
            self.submitted = True
            self.offramp_id = quote["id"]
            sent = self.client.submit(self.chain, quote["intent"], user_sig, permit_fields)
        except (QrError, VerificationError) as e:
            return PaymentResult("REFUSED", detail=str(e), stages=stages)
        except PaymentAborted as e:
            return PaymentResult("STOPPED", detail=str(e), stages=stages)

        offramp_id = quote["id"]
        step(f"submitted, polling {offramp_id[:10]}")
        status = self._poll(offramp_id, quote.get("viewToken"))
        step(f"status {status}")
        return PaymentResult(
            status, offramp_id, sent.get("txHash"), total, payment.upi_id,
            f"{quote['display']['amount']} {quote['display']['symbol']}", stages=stages,
        )

    def _poll(self, id_: str, view_token: str | None) -> str:
        deadline = self.clock() + self.poll_timeout_sec
        status = "SUBMITTED"
        while self.clock() < deadline:
            try:
                status = self.client.status(id_, view_token)["status"]
            except Exception:  # the payment is already submitted, so a network blip must not lose track of it
                self.sleep(self.poll_sec)
                continue
            if status in FINAL_OK or status in FINAL_BAD:
                return status
            self.sleep(self.poll_sec)
        return f"TIMED_OUT_{status}"
