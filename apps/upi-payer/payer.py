"""Pays one UPI payment from Google Pay's UPI Lite balance on an ADB-connected phone.

Every attempt ends in one of three states:

    paid      success text and the amount were on screen
    failed    definitely not paid: Pay was never tapped, or the app showed an explicit failure
    unknown   anything else after the Pay tap; a person must check the phone

It is a dry run unless UPI_LITE_LIVE=1. A dry run touches no phone and moves no money.

Safety limits (see "Config"):
  - at most Rs 1,000 per payment and Rs 1,000 per rolling 24 h by default, in integer paise;
  - each id can pay at most once, recorded in its own ledger before the phone is touched;
  - before tapping Pay it reads the screen and requires the right amount, payee and UPI Lite as the source,
    so a stale gallery image or a misplaced tap stops the payment instead of paying the wrong QR.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote_plus

EXIT_PAID = 0
EXIT_NOT_PAID = 1
EXIT_UNKNOWN = 75

GPAY = "com.google.android.apps.nbu.paisa.user"
APP_DIR = Path(__file__).resolve().parent
PHONE_DIR = "/sdcard/Pictures"
PHONE_UI_DUMP = "/data/local/tmp/rupeerail_ui.xml"  # not visible to other apps, unlike /sdcard
ONE_DAY = 86_400

# Tap coordinates for a 1080x2408 screen (Samsung SM-M336B). Override with UPI_LITE_TAPS for another phone.
DEFAULT_TAPS = {
    "scan_any_qr": (165, 765),
    "upload_from_gallery": (542, 1288),
    "image": (180, 1350),  # first thumbnail in the gallery picker
    "done": (871, 2196),
    "pay": (572, 2054),
}
# Seconds to wait AFTER each step. Override with UPI_LITE_DELAYS if the phone is slow.
DEFAULT_DELAYS = {
    "launch": 3.5,
    "scan_any_qr": 2.0,
    "upload_from_gallery": 2.0,
    "image": 1.5,
    "done": 3.0,
    "pay": 5.0,
}
BEFORE_PAY_STEPS = ("scan_any_qr", "upload_from_gallery", "image", "done")

VPA_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z0-9]{2,64}$")
LITE_RE = re.compile(r"\bupi\s*lite\b", re.IGNORECASE)
PENDING_RE = re.compile(r"\b(processing|pending|in progress|please wait)\b", re.IGNORECASE)
SUCCESS_RE = re.compile(r"\b(paid|payment successful|payment complete|transaction successful|successful)\b", re.IGNORECASE)
FAILURE_RE = re.compile(r"\b(limit exceeded|failed|declined|couldn['’]?t|could not|unable to)\b", re.IGNORECASE)
PIN_RE = re.compile(r"\benter\b.*\bupi pin\b", re.IGNORECASE)  # a PIN we never type, so no money moved
UTR_RE = re.compile(r"\b(?:upi (?:transaction|ref(?:erence)?)\b[^\d]{0,24}|utr\b[^\d]{0,12})(\d{12})\b", re.IGNORECASE)


class NotPaid(Exception):
    """The payment stopped before any money could move: safe to refund."""


class Adb:
    def __init__(self, binary: str = "adb", serial: str | None = None, timeout: float = 15.0) -> None:
        self.prefix = [binary] + (["-s", serial] if serial else [])
        self.timeout = timeout

    def run(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(self.prefix + list(args), capture_output=True, text=True, timeout=self.timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"adb {args[0] if args else ''} failed: {r.stderr.strip()[:200]}")
        return r.stdout


# ---- config ---------------------------------------------------------------------------------


def _inr_to_paise(raw: str) -> int:
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"not an INR amount: {raw!r}") from None
    if not v.is_finite() or v < 0:
        raise ValueError(f"not an INR amount: {raw!r}")
    return int(v * 100)


def _json_env(env: dict, name: str, default: dict) -> dict:
    raw = env.get(name)
    return {**default, **json.loads(raw)} if raw else dict(default)


@dataclass
class Config:
    live: bool = False
    max_paise: int = 100_000  # Rs 1,000 per payment
    daily_cap_paise: int = 100_000  # Rs 1,000 per rolling 24 h
    allowlist: set[str] = field(default_factory=set)
    verify: str = "strict"  # strict: amount, payee, UPI Lite. basic: amount, payee. off: tap blindly.
    relaunch: bool = False
    back_after: int = 0
    db_path: str = str(APP_DIR / "data" / "payer.sqlite")
    taps: dict = field(default_factory=lambda: dict(DEFAULT_TAPS))
    delays: dict = field(default_factory=lambda: dict(DEFAULT_DELAYS))
    adb_binary: str = "adb"
    adb_serial: str | None = None
    verify_wait_sec: float = 6.0
    result_wait_sec: float = 20.0
    poll_sec: float = 1.0
    default_budget_sec: float = 120.0
    exit_margin_sec: float = 5.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        verify = (e.get("UPI_LITE_VERIFY") or "strict").lower()
        if verify not in ("strict", "basic", "off"):
            raise ValueError("UPI_LITE_VERIFY must be strict, basic or off")
        return cls(
            live=e.get("UPI_LITE_LIVE") == "1",
            max_paise=_inr_to_paise(e.get("UPI_LITE_MAX_INR") or "1000"),
            daily_cap_paise=_inr_to_paise(e.get("UPI_LITE_DAILY_CAP_INR") or "1000"),
            allowlist={v.strip().lower() for v in (e.get("UPI_LITE_ALLOWLIST") or "").split(",") if v.strip()},
            verify=verify,
            relaunch=e.get("UPI_LITE_RELAUNCH") == "1",
            back_after=int(e.get("UPI_LITE_BACK_AFTER") or 0),
            db_path=e.get("UPI_LITE_DB") or str(APP_DIR / "data" / "payer.sqlite"),
            taps=_json_env(e, "UPI_LITE_TAPS", DEFAULT_TAPS),
            delays=_json_env(e, "UPI_LITE_DELAYS", DEFAULT_DELAYS),
            adb_binary=e.get("UPI_LITE_ADB") or "adb",
            adb_serial=e.get("UPI_LITE_ADB_SERIAL") or None,
        )


# ---- pure logic -----------------------------------------------------------------------------


def format_paise(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def build_upi_uri(*, upi_id: str, payee_name: str | None, paise: int, qr_text: str | None) -> str:
    """The URI to encode. A merchant's raw QR is kept as is (tr/mc/sign survive), adding `am` only if it has none."""
    if not qr_text:
        name = f"&pn={quote(payee_name)}" if payee_name else ""
        return f"upi://pay?pa={upi_id}{name}&am={format_paise(paise)}&cu=INR&tn=RupeeRail"
    uri = qr_text.strip()
    head, _, query = uri.partition("?")
    params = [p for p in query.split("&") if p]

    def first(name: str) -> str | None:  # first occurrence wins, as in app.qr
        return next((p.partition("=")[2] for p in params if p.partition("=")[0].lower() == name), None)

    if unquote_plus(first("pa") or "").lower() != upi_id.lower():
        raise NotPaid("raw QR payee differs from the job's payee")
    if first("am"):
        return uri  # the listener has already checked that a present `am` equals the signed amount
    kept = [p for p in params if p.partition("=")[0].lower() != "am"]
    return f"{head}?{'&'.join(kept + [f'am={format_paise(paise)}'])}"


def amount_visible(texts: list[str], paise: int) -> bool:
    """True if a node shows exactly this amount: with a currency mark, or as the node's whole text."""
    rupees, cents = paise // 100, paise % 100
    if cents == 0:
        number = rf"{rupees}(?:\.0{{1,2}})?"
    elif cents % 10 == 0:
        number = rf"{rupees}\.{cents // 10}0?"
    else:
        number = rf"{rupees}\.{cents:02d}"
    tail = r"(?!\d|\.\d)"
    marked = re.compile(rf"(?:₹|\brs\.?|\binr)\s*{number}{tail}", re.IGNORECASE)
    alone = re.compile(rf"^\s*{number}\s*$")
    for raw in texts:
        t = raw.replace(",", "")
        if marked.search(t) or alone.match(t):
            return True
    return False


def payee_visible(texts: list[str], upi_id: str, payee_name: str | None) -> bool:
    blob = "\n".join(texts).lower()
    return upi_id.lower() in blob or bool(payee_name and payee_name.strip().lower() in blob)


def check_pay_screen(texts: list[str] | None, mode: str, *, upi_id: str, payee_name: str | None, paise: int) -> str | None:
    """Returns what is wrong with the pay screen, or None if Pay may be tapped."""
    if mode == "off":
        return None
    if not texts:
        return "screen text unreadable"
    if not amount_visible(texts, paise):
        return f"amount Rs {format_paise(paise)} not on screen"
    if not payee_visible(texts, upi_id, payee_name):
        return f"payee {upi_id} not on screen"
    if mode == "strict" and not any(LITE_RE.search(t) for t in texts):
        return "UPI Lite not shown as the payment source"
    return None


def classify_result(before: list[str] | None, after: list[str] | None, paise: int) -> tuple[str, str]:
    """Classifies the screen after Pay. Only text new since the tap counts: chat history may already say "Paid"."""
    if not after:
        return "unknown", "screen text unreadable"
    seen = set(before or [])
    fresh = [t for t in after if t not in seen]
    success = any(SUCCESS_RE.search(t) for t in fresh)
    failure = any(FAILURE_RE.search(t) for t in fresh)
    if success and failure:
        return "unknown", "both success and failure text on screen"
    if any(PENDING_RE.search(t) for t in fresh):
        return "unknown", "payment still processing"
    if failure:
        return "failed", "failure text on screen"
    if any(PIN_RE.search(t) for t in after):
        return "failed", "asked for a UPI PIN (UPI Lite not used); no PIN was entered"
    if success:
        if amount_visible(after, paise):
            return "paid", "success text and amount on screen"
        return "unknown", "success text without the expected amount"
    return "unknown", "no result text on screen"


def find_utr(before: list[str] | None, after: list[str] | None) -> str | None:
    """A labelled UTR new since the Pay tap. The label and the number may be separate nodes."""
    seen = set(before or [])
    m = UTR_RE.search("\n".join(t for t in (after or []) if t not in seen))
    return m.group(1) if m else None


def parse_ui_dump(xml_text: str) -> list[str] | None:
    """Visible text from a uiautomator dump: only text and content-desc, never resource ids."""
    start = xml_text.find("<?xml")
    if start < 0:
        start = xml_text.find("<hierarchy")
    if start < 0:
        return None
    try:
        root = ET.fromstring(xml_text[start:])
    except ET.ParseError:
        return None
    texts = []
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            value = (node.get(attr) or "").strip()
            if value:
                texts.append(value)
    return texts or None


# ---- ledger: idempotency and the daily cap ----------------------------------------------------


class Ledger:
    SPENDING = ("running", "paid", "unknown")

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS payments (key TEXT PRIMARY KEY, upi_id TEXT, paise INTEGER, status TEXT, "
            "detail TEXT, utr TEXT, created REAL, updated REAL)"
        )

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT status, detail, utr FROM payments WHERE key = ?", (key,)).fetchone()
        return dict(zip(("status", "detail", "utr"), row)) if row else None

    def spent_paise(self) -> int:
        marks = ",".join("?" * len(self.SPENDING))
        row = self.db.execute(
            f"SELECT COALESCE(SUM(paise), 0) FROM payments WHERE created > ? AND status IN ({marks})",
            (self.clock() - ONE_DAY, *self.SPENDING),
        ).fetchone()
        return int(row[0])

    def reserve(self, key: str, upi_id: str, paise: int, daily_cap: int, status: str = "running") -> dict | None:
        """Atomically checks the cap and records the attempt. Returns the old row if this key was already used."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get(key)
            if existing:
                return existing
            if status == "running" and self.spent_paise() + paise > daily_cap:
                raise NotPaid(f"daily cap Rs {format_paise(daily_cap)} reached")
            now = self.clock()
            self.db.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?)", (key, upi_id, paise, status, "", None, now, now))
            return None
        finally:
            self.db.execute("COMMIT")

    def finish(self, key: str, status: str, detail: str, utr: str | None = None) -> None:
        self.db.execute("UPDATE payments SET status = ?, detail = ?, utr = ?, updated = ? WHERE key = ?",
                        (status, detail, utr, self.clock(), key))


# ---- phone ----------------------------------------------------------------------------------


class Phone:
    def __init__(self, adb: Any, cfg: Config, sleep: Callable[[float], None] = time.sleep) -> None:
        self.adb, self.cfg, self.sleep = adb, cfg, sleep

    def tap(self, step: str) -> None:
        x, y = self.cfg.taps[step]
        self.adb.run("shell", "input", "tap", str(x), str(y))
        self.sleep(float(self.cfg.delays.get(step, 1.0)))

    def foreground(self) -> str:
        return self.adb.run("shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", check=False)

    def require_gpay_foreground(self) -> None:
        if GPAY not in self.foreground():
            raise NotPaid("Google Pay is not in the foreground (is the phone unlocked?)")

    def screen_texts(self) -> list[str] | None:
        """Best effort: None if the screen can't be read, which callers treat as "don't know"."""
        try:
            self.adb.run("shell", "uiautomator", "dump", PHONE_UI_DUMP, check=False)
            texts = parse_ui_dump(self.adb.run("shell", "cat", PHONE_UI_DUMP, check=False))
            self.adb.run("shell", "rm", "-f", PHONE_UI_DUMP, check=False)  # it can hold payment details
            return texts
        except Exception:
            return None

    def push_qr(self, uri: str, key: str) -> None:
        import segno  # imported here so a dry run works without it

        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)[:40]
        remote = f"{PHONE_DIR}/upi_demo_{safe}.png"
        with tempfile.TemporaryDirectory(prefix="upi-lite-") as d:
            local = os.path.join(d, "qr.png")
            segno.make(uri, error="m").save(local, scale=12, border=4)
            # Keep only this QR in the gallery so it is the newest image. check_pay_screen catches it if not.
            self.clear_qrs()
            self.adb.run("push", local, remote)
        self.adb.run("shell", "content", "call", "--uri", "content://media/", "--method", "scan_volume",
                     "--arg", "external_primary", check=False)
        self.adb.run("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                     "-d", f"file://{remote}", check=False)

    def clear_qrs(self) -> None:
        self.adb.run("shell", f"rm -f {PHONE_DIR}/upi_demo_*.png", check=False)

    def to_pay_screen(self, uri: str, key: str) -> None:
        self.push_qr(uri, key)
        self.adb.run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        if self.cfg.relaunch:
            self.adb.run("shell", "am", "force-stop", GPAY)
            self.adb.run("shell", "monkey", "-p", GPAY, "-c", "android.intent.category.LAUNCHER", "1")
            self.sleep(float(self.cfg.delays["launch"]))
        self.require_gpay_foreground()
        for step in BEFORE_PAY_STEPS:
            self.tap(step)

    def wait_for_pay_screen(self, *, upi_id: str, payee_name: str | None, paise: int) -> tuple[list[str] | None, str | None]:
        """Polls until the pay screen checks out. Returns (texts, problem), with problem None when it does."""
        if self.cfg.verify == "off":
            return self.screen_texts(), None
        texts, problem, waited = None, "screen text unreadable", 0.0
        while True:
            texts = self.screen_texts()
            problem = check_pay_screen(texts, self.cfg.verify, upi_id=upi_id, payee_name=payee_name, paise=paise)
            if problem is None or waited >= self.cfg.verify_wait_sec:
                return texts, problem
            self.sleep(self.cfg.poll_sec)
            waited += self.cfg.poll_sec

    def back(self, times: int = 1) -> None:
        for _ in range(times):
            self.adb.run("shell", "input", "keyevent", "KEYCODE_BACK", check=False)
            self.sleep(0.8)


# ---- one payment ----------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[upi-lite] {msg}", file=sys.stderr)


def _lock_phone(path: str, deadline: float, clock: Callable[[], float], sleep: Callable[[float], None]):
    """One phone means one payment at a time, across processes. Released on exit, even after a kill."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if clock() >= deadline:
                handle.close()
                raise NotPaid("phone busy with another payment") from None
            sleep(0.25)


def _validate(job: dict, cfg: Config) -> tuple[str, str, str | None, int]:
    key = str(job.get("idempotencyKey") or "")
    upi_id = str(job.get("upiId") or "").strip()
    if not key:
        raise NotPaid("job has no idempotencyKey")
    if not VPA_RE.match(upi_id):
        raise NotPaid("invalid UPI ID")
    paise = job.get("inrPaise")
    if isinstance(paise, bool) or not isinstance(paise, int) or paise <= 0:
        raise NotPaid("inrPaise must be a positive integer")
    if paise > cfg.max_paise:
        raise NotPaid(f"Rs {format_paise(paise)} is over the Rs {format_paise(cfg.max_paise)} per-payment limit")
    if cfg.allowlist and upi_id.lower() not in cfg.allowlist:
        raise NotPaid("UPI ID is not in UPI_LITE_ALLOWLIST")
    return key, upi_id, job.get("payeeName") or None, paise


def _replay(row: dict) -> tuple[int, dict]:
    status = row["status"]
    if status == "paid":
        return EXIT_PAID, {"status": status, "utr": row["utr"], "replay": True}
    if status in ("failed", "rejected"):
        return EXIT_NOT_PAID, {"status": status, "detail": row["detail"], "replay": True}
    if status == "dry_run":
        return EXIT_PAID, {"status": status, "replay": True}
    # "running" means an earlier attempt died part way: the phone may already have paid.
    return EXIT_UNKNOWN, {"status": "unknown", "detail": f"earlier attempt left {status}", "replay": True}


def run_job(job: dict, cfg: Config, adb: Any | None = None, *, clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> tuple[int, dict]:
    """Returns (exit code, result). See the module docstring for what each exit code promises."""
    started = clock()
    budget = float(job.get("timeoutSec") or cfg.default_budget_sec)
    deadline = started + budget - cfg.exit_margin_sec

    try:
        key, upi_id, payee_name, paise = _validate(job, cfg)
    except NotPaid as e:
        return EXIT_NOT_PAID, {"status": "rejected", "detail": str(e)}

    ledger = Ledger(cfg.db_path, clock=clock)
    old = ledger.get(key)
    if old:
        return _replay(old)

    if not cfg.live:
        old = ledger.reserve(key, upi_id, paise, cfg.daily_cap_paise, status="dry_run")
        if old:
            return _replay(old)
        ledger.finish(key, "dry_run", "no phone touched")
        log(f"DRY RUN: would pay Rs {format_paise(paise)} to {upi_id}. Set UPI_LITE_LIVE=1 to pay for real.")
        return EXIT_PAID, {"status": "dry_run"}

    after_pay_sec = float(cfg.delays["pay"]) + cfg.result_wait_sec
    phone = Phone(adb or Adb(cfg.adb_binary, cfg.adb_serial), cfg, sleep=sleep)
    pay_tapped = False
    reserved = False
    lock_handle = None
    short_on_time = f"not enough time to confirm the result within {budget:g}s"
    try:
        before_pay_sec = sum(float(cfg.delays[s]) for s in BEFORE_PAY_STEPS)
        latest_start = deadline - after_pay_sec - before_pay_sec
        if clock() > latest_start:
            raise NotPaid(short_on_time)
        lock_handle = _lock_phone(cfg.db_path + ".lock", latest_start, clock, sleep)

        old = ledger.reserve(key, upi_id, paise, cfg.daily_cap_paise)
        if old:
            return _replay(old)
        reserved = True

        uri = build_upi_uri(upi_id=upi_id, payee_name=payee_name, paise=paise, qr_text=job.get("qrText"))
        phone.to_pay_screen(uri, key)
        before, problem = phone.wait_for_pay_screen(upi_id=upi_id, payee_name=payee_name, paise=paise)
        if problem:
            phone.back(2)
            raise NotPaid(f"pay screen check failed: {problem}")
        phone.require_gpay_foreground()
        # Last check before the point of no return: there must be time left to read the result.
        if clock() + after_pay_sec > deadline:
            phone.back(2)
            raise NotPaid(short_on_time)

        pay_tapped = True
        phone.tap("pay")

        wait_until = min(clock() + cfg.result_wait_sec, deadline)
        while True:
            after = phone.screen_texts()
            status, detail = classify_result(before, after, paise)
            if status != "unknown" or clock() >= wait_until:
                break
            sleep(cfg.poll_sec)

        utr = find_utr(before, after) if status == "paid" else None
        ledger.finish(key, status, detail, utr)
        # The outcome is recorded: a cleanup problem from here on must not change it.
        if cfg.back_after:
            _quietly(lambda: phone.back(cfg.back_after))
        _quietly(phone.clear_qrs)
        result = {"status": status, "detail": detail}
        if utr:
            result["utr"] = utr
        return {"paid": EXIT_PAID, "failed": EXIT_NOT_PAID}.get(status, EXIT_UNKNOWN), result
    except NotPaid as e:
        if reserved:
            ledger.finish(key, "failed", str(e))
            _quietly(phone.clear_qrs)
        return EXIT_NOT_PAID, {"status": "failed", "detail": str(e)}
    except BaseException as e:  # including Ctrl-C: a non-zero exit from it would otherwise read as "refund"
        detail = f"{type(e).__name__}: {str(e)[:200]}"
        if pay_tapped:
            ledger.finish(key, "unknown", detail)
            return EXIT_UNKNOWN, {"status": "unknown", "detail": f"error after tapping Pay: {detail}"}
        if reserved:
            ledger.finish(key, "failed", detail)
            _quietly(phone.clear_qrs)
        return EXIT_NOT_PAID, {"status": "failed", "detail": f"error before tapping Pay: {detail}"}
    finally:
        if lock_handle:
            lock_handle.close()


def _quietly(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        pass


# ---- tools ----------------------------------------------------------------------------------


def cmd_check(cfg: Config, adb: Any) -> int:
    print(f"mode:      {'LIVE' if cfg.live else 'dry run'}")
    print(f"limits:    Rs {format_paise(cfg.max_paise)} per payment, Rs {format_paise(cfg.daily_cap_paise)} per 24 h")
    print(f"verify:    {cfg.verify}")
    print(f"allowlist: {', '.join(sorted(cfg.allowlist)) or '(none: any UPI ID)'}")
    print(f"spent:     Rs {format_paise(Ledger(cfg.db_path).spent_paise())} in the last 24 h")
    phone = Phone(adb, cfg)
    focus = phone.foreground()
    device_ok = bool(focus.strip())
    print(f"device:    {'connected' if device_ok else 'NOT FOUND or locked'}")
    gpay_ok = GPAY in focus
    print(f"gpay:      {'in the foreground' if gpay_ok else 'NOT in the foreground'}")
    texts = phone.screen_texts()
    print(f"screen:    {'readable, ' + str(len(texts)) + ' text items' if texts else 'NOT readable (see --dump-screen)'}")
    return 0 if device_ok and gpay_ok and texts else 1


def cmd_dump(cfg: Config, adb: Any) -> int:
    texts = Phone(adb, cfg).screen_texts()
    if not texts:
        print("No text could be read from this screen. Verification can't work here; see UPI_LITE_VERIFY.", file=sys.stderr)
        return 1
    print("\n".join(texts))
    return 0


def cmd_rehearse(cfg: Config, adb: Any, upi_id: str, inr: str, payee_name: str | None,
                 sleep: Callable[[float], None] = time.sleep) -> int:
    """Walks the flow up to the Pay button, checks the screen, then backs out. Never taps Pay."""
    paise = _inr_to_paise(inr)
    if not VPA_RE.match(upi_id) or paise <= 0:
        print("need a valid --upi-id and a positive --inr", file=sys.stderr)
        return 2
    phone = Phone(adb, cfg, sleep=sleep)
    try:
        phone.to_pay_screen(build_upi_uri(upi_id=upi_id, payee_name=payee_name, paise=paise, qr_text=None), "rehearsal")
    except (NotPaid, RuntimeError) as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        phone.clear_qrs()
        return 1
    texts, problem = phone.wait_for_pay_screen(upi_id=upi_id, payee_name=payee_name, paise=paise)
    print("--- text on the pay screen ---")
    print("\n".join(texts or ["(unreadable)"]))
    print("------------------------------")
    x, y = cfg.taps["pay"]
    print(f"Pay button is expected at ({x}, {y}). Compare it with the screen now: this is where a live run would tap.")
    print(f"PASS: a live run would tap Pay ({cfg.verify})" if not problem else f"FAIL: a live run would stop and refund: {problem}")
    phone.sleep(5)
    phone.back(2)
    phone.clear_qrs()
    return 0 if not problem else 1
