"""The payment flow against a simulated phone. No real device, and no test can tap a real one."""

from __future__ import annotations

import stat

import pytest

import payer as ul

ID = "0x" + "c1" * 32
VPA = "chaipoint@okaxis"
QR_TEXT = "upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point"
PAY_SCREEN = ["Paying Chai Point", "chaipoint@okaxis", "₹10", "UPI Lite", "Pay"]
PAID_SCREEN = ["Paid", "₹10.00", "to Chai Point"]
HOME_SCREEN = ["Scan any\nQR code", "UPI Lite\n₹96", "Send money"]  # one node holds both lines, as on a real phone


def dump(texts):
    nodes = "".join(f'<node text="{t}" content-desc="" resource-id="com.x:id/failed"/>' for t in texts)
    return f'UI hierchary dumped to: x\n<?xml version="1.0"?><hierarchy>{nodes}</hierarchy>'


class FakePhone:
    """Answers adb commands like a phone: Pay moves from `pay_screen` to `result`."""

    def __init__(self, pay_screen=PAY_SCREEN, result=PAID_SCREEN, foreground=ul.GPAY, die_on_pay=False, die_after_pay=False,
                 start="home", can_open=True):
        self.pay_screen, self.result, self.fg = pay_screen, result, foreground
        self.screen, self.can_open, self.backs = start, can_open, 0
        self.die_on_pay, self.die_after_pay = die_on_pay, die_after_pay
        self.taps, self.pushed, self.calls = [], [], []
        self.paid = False

    def run(self, *args, check=True):
        self.calls.append(args)
        if args[0] == "push":
            self.pushed.append(args[2])
            return ""
        joined = " ".join(args)
        if self.paid and self.die_after_pay:
            raise RuntimeError("device offline")
        if "input tap" in joined:
            self.taps.append((int(args[3]), int(args[4])))
            if self.taps[-1] == ul.DEFAULT_TAPS["done"]:
                self.screen = "pay"
            if self.taps[-1] == ul.DEFAULT_TAPS["pay"]:
                if self.die_on_pay:
                    raise RuntimeError("device offline")
                self.paid = True
                self.screen = "result"
            return ""
        if "KEYCODE_BACK" in joined:
            self.backs += 1
            self.screen = "home" if self.screen != "other2" else "other"
            return ""
        if "statusbar collapse" in joined:
            if self.fg == "panel":
                self.fg = ul.GPAY
            return ""
        if "monkey" in joined:
            if self.fg == "panel":
                return ""  # reopening an app doesn't close the panel
            if self.can_open:
                self.fg, self.screen = ul.GPAY, "home" if self.screen == "launcher" else self.screen
            return ""
        if "dumpsys" in joined:
            return f"mCurrentFocus=Window{{1 u0 {self.fg}/.Main}}"
        if args[:2] == ("shell", "cat"):
            texts = {"result": self.result, "pay": self.pay_screen, "home": HOME_SCREEN}.get(self.screen, ["Some other screen"])
            return dump(texts) if texts else ""
        return ""

    @property
    def pay_tapped(self):
        return ul.DEFAULT_TAPS["pay"] in self.taps


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def cfg(tmp_path):
    return ul.Config(live=True, db_path=str(tmp_path / "ledger.sqlite"))


def job(key=ID, paise=1000, **extra):
    return {"idempotencyKey": key, "upiId": VPA, "payeeName": "Chai Point", "inrPaise": paise, "timeoutSec": 120, **extra}


def run(cfg, phone, j=None, clock=None):
    clock = clock or Clock()
    return ul.run_job(j or job(), cfg, phone, clock=clock, sleep=clock.sleep)


# ---- the three outcomes -------------------------------------------------------------------------


def test_paid_taps_pay_exactly_once_after_the_gallery_steps(cfg):
    p = FakePhone()
    code, res = run(cfg, p)
    assert code == ul.EXIT_PAID and res["status"] == "paid"
    assert p.taps == [ul.DEFAULT_TAPS[s] for s in ("scan_any_qr", "upload_from_gallery", "image", "done", "pay")]


def test_utr_on_the_result_screen_is_reported(cfg):
    p = FakePhone(result=PAID_SCREEN + ["UPI transaction ID", "123456789012"])
    assert run(cfg, p)[1]["utr"] == "123456789012"


def test_explicit_failure_after_pay_is_not_paid(cfg):
    code, res = run(cfg, FakePhone(result=["Payment failed", "₹10"]))
    assert code == ul.EXIT_NOT_PAID and res["status"] == "failed"


@pytest.mark.parametrize("result", [
    None,                                  # unreadable
    ["Some other screen"],                 # no result text
    ["Payment processing", "₹10"],         # may still go through
    ["Paid", "Payment failed", "₹10"],     # contradictory
    ["Paid", "₹25"],                       # success, but for another amount
])
def test_anything_unclear_after_pay_is_unknown_never_refunded(cfg, result):
    code, res = run(cfg, FakePhone(result=result))
    assert code == ul.EXIT_UNKNOWN and res["status"] == "unknown"


@pytest.mark.parametrize("kw", [{"die_on_pay": True}, {"die_after_pay": True}])
def test_adb_dying_at_or_after_the_pay_tap_is_unknown(cfg, kw):
    # die_after_pay's screen reads are swallowed as "unreadable"; die_on_pay raises out of the tap itself
    code, res = run(cfg, FakePhone(**kw))
    assert code == ul.EXIT_UNKNOWN and res["status"] == "unknown"


def test_interrupt_after_pay_is_unknown(cfg, monkeypatch):
    p = FakePhone()
    monkeypatch.setattr(ul, "classify_result", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert run(cfg, p)[0] == ul.EXIT_UNKNOWN


# ---- the checks before Pay ----------------------------------------------------------------------


@pytest.mark.parametrize("screen,why", [
    (["Paying Someone Else", "other@ybl", "₹10", "UPI Lite", "Pay"], "payee"),   # stale gallery image
    (["Paying Chai Point", "chaipoint@okaxis", "₹100", "UPI Lite", "Pay"], "amount"),
    (["Paying Chai Point", "chaipoint@okaxis", "₹10", "HDFC Bank", "Pay"], "UPI Lite"),
    (None, "unreadable"),
])
def test_wrong_pay_screen_never_taps_pay_and_is_safe_to_refund(cfg, screen, why):
    p = FakePhone(pay_screen=screen)
    code, res = run(cfg, p)
    assert not p.pay_tapped and code == ul.EXIT_NOT_PAID and why in res["detail"]


def test_verify_basic_does_not_require_lite_text(cfg):
    cfg.verify = "basic"
    assert run(cfg, FakePhone(pay_screen=["chaipoint@okaxis", "₹10", "Pay"]))[0] == ul.EXIT_PAID


def test_gpay_not_in_foreground_stops_before_any_tap(cfg):
    p = FakePhone(foreground="com.android.systemui", can_open=False)  # locked: Google Pay can't come forward
    code, res = run(cfg, p)
    assert code == ul.EXIT_NOT_PAID and p.taps == [] and "foreground" in res["detail"]


def test_consecutive_payments_get_back_to_home_on_their_own(cfg):
    p = FakePhone()
    clock = Clock()
    assert run(cfg, p, job("first"), clock)[0] == ul.EXIT_PAID
    assert p.screen == "result"  # a payment leaves the phone on the result screen
    p.paid = False
    p.taps.clear()
    assert run(cfg, p, job("second"), clock)[0] == ul.EXIT_PAID
    assert p.backs == 1 and p.taps[0] == ul.DEFAULT_TAPS["scan_any_qr"]


def test_starting_from_another_screen_backs_out_before_any_tap(cfg):
    p = FakePhone(start="other2")
    assert run(cfg, p)[0] == ul.EXIT_PAID
    assert p.backs == 2


def test_google_pay_in_the_background_is_reopened(cfg):
    p = FakePhone(foreground="launcher", start="launcher")
    assert run(cfg, p)[0] == ul.EXIT_PAID
    assert any("monkey" in " ".join(c) for c in p.calls)


def test_the_notification_panel_gets_closed_instead_of_failing_the_payment(cfg):
    p = FakePhone(foreground="panel")
    assert run(cfg, p)[0] == ul.EXIT_PAID
    assert any("statusbar" in " ".join(c) for c in p.calls)


def test_a_screen_it_cant_leave_never_gets_tapped(cfg):
    class Stuck(FakePhone):
        def run(self, *args, check=True):
            out = super().run(*args, check=check)
            if "KEYCODE_BACK" in " ".join(args):
                self.screen = "other"
            return out
    p = Stuck(start="other")
    code, res = run(cfg, p)
    assert code == ul.EXIT_NOT_PAID and p.taps == [] and "home screen" in res["detail"]
    assert p.pushed == []


def test_unreadable_screen_never_gets_tapped(cfg):
    class Blind(FakePhone):
        def run(self, *args, check=True):
            out = super().run(*args, check=check)
            return "" if args[:2] == ("shell", "cat") else out
    p = Blind()
    code, _ = run(cfg, p)
    assert code == ul.EXIT_NOT_PAID and p.taps == []


def test_a_failed_check_returns_to_home_for_the_next_payment(cfg):
    p = FakePhone(pay_screen=["other@ybl", "₹10", "UPI Lite"])
    assert run(cfg, p)[0] == ul.EXIT_NOT_PAID
    assert p.screen == "home" and not p.pay_tapped


def test_short_backend_timeout_fails_closed_before_touching_the_phone(cfg):
    p = FakePhone()
    code, res = run(cfg, p, job(timeoutSec=30))
    assert code == ul.EXIT_NOT_PAID and p.calls == [] and "not enough time" in res["detail"]


def test_time_running_out_on_the_pay_screen_does_not_tap_pay(cfg):
    p = FakePhone()
    clock = Clock()

    def slow_sleep(s):
        clock.t += s * 4  # a slow phone: every wait takes four times as long
    code, res = ul.run_job(job(timeoutSec=60), cfg, p, clock=clock, sleep=slow_sleep)
    assert code == ul.EXIT_NOT_PAID and not p.pay_tapped and "not enough time" in res["detail"]


def test_adb_error_before_pay_is_not_paid(cfg):
    class Dead(FakePhone):
        def run(self, *a, check=True):
            if a[0] == "push":
                raise RuntimeError("no devices")
            return super().run(*a, check=check)
    p = Dead()
    code, res = run(cfg, p)
    assert code == ul.EXIT_NOT_PAID and not p.pay_tapped


# ---- limits -------------------------------------------------------------------------------------


def test_default_limits_are_1000_per_payment_and_per_day():
    c = ul.Config.from_env({})
    assert c.max_paise == 100_000 and c.daily_cap_paise == 100_000 and not c.live and c.verify == "strict"


@pytest.mark.parametrize("paise", [100_001, 0, -5, True, "1000", 10.5])
def test_bad_or_over_limit_amounts_are_rejected_without_touching_the_phone(cfg, paise):
    p = FakePhone()
    assert run(cfg, p, job(paise=paise))[0] == ul.EXIT_NOT_PAID and p.calls == []


def test_exactly_1000_is_allowed(cfg):
    screen = ["chaipoint@okaxis", "₹1,000", "UPI Lite"]
    assert run(cfg, FakePhone(pay_screen=screen, result=["Paid", "₹1,000.00"]), job(paise=100_000))[0] == ul.EXIT_PAID


def test_daily_cap_counts_paid_and_unknown_but_not_failed(cfg):
    cfg.daily_cap_paise = 2500
    clock = Clock()
    assert run(cfg, FakePhone(result=["Payment failed"]), job("a"), clock)[0] == ul.EXIT_NOT_PAID  # released
    assert run(cfg, FakePhone(), job("b"), clock)[0] == ul.EXIT_PAID  # Rs 10 spent
    assert run(cfg, FakePhone(result=None), job("c"), clock)[0] == ul.EXIT_UNKNOWN  # Rs 10 assumed spent
    p = FakePhone()
    code, res = run(cfg, p, job("d"), clock)
    assert code == ul.EXIT_NOT_PAID and "daily cap" in res["detail"] and p.calls == []


def test_daily_cap_is_a_rolling_24_hours(cfg):
    cfg.daily_cap_paise = 1000
    clock = Clock()
    assert run(cfg, FakePhone(), job("a"), clock)[0] == ul.EXIT_PAID
    assert run(cfg, FakePhone(), job("b"), clock)[0] == ul.EXIT_NOT_PAID
    clock.t += ul.ONE_DAY + 1
    assert run(cfg, FakePhone(), job("c"), clock)[0] == ul.EXIT_PAID


def test_allowlist(cfg):
    cfg.allowlist = {"someone@ybl"}
    p = FakePhone()
    assert run(cfg, p)[0] == ul.EXIT_NOT_PAID and p.calls == []
    cfg.allowlist = {VPA}
    assert run(cfg, FakePhone())[0] == ul.EXIT_PAID


# ---- idempotency --------------------------------------------------------------------------------


@pytest.mark.parametrize("result,expected", [
    (PAID_SCREEN, ul.EXIT_PAID),
    (["Payment failed"], ul.EXIT_NOT_PAID),
    (None, ul.EXIT_UNKNOWN),
])
def test_replaying_a_key_never_touches_the_phone_again(cfg, result, expected):
    assert run(cfg, FakePhone(result=result))[0] == expected
    second = FakePhone()
    code, res = run(cfg, second)
    assert code == expected and res["replay"] and second.calls == []


def test_crash_during_payment_replays_as_unknown(cfg):
    ledger = ul.Ledger(cfg.db_path)
    ledger.reserve(ID, VPA, 1000, 100_000)  # written, then the process died
    p = FakePhone()
    assert run(cfg, p)[0] == ul.EXIT_UNKNOWN and p.calls == []


# ---- dry run ------------------------------------------------------------------------------------


def test_dry_run_is_the_default_and_uses_no_adb(tmp_path):
    cfg = ul.Config.from_env({"UPI_LITE_DB": str(tmp_path / "l.sqlite")})
    p = FakePhone()
    code, res = ul.run_job(job(), cfg, p)
    assert code == ul.EXIT_PAID and res["status"] == "dry_run" and p.calls == []
    assert ul.Ledger(cfg.db_path).spent_paise() == 0


@pytest.mark.parametrize("env,live", [
    ({"UPI_LITE_LIVE": "1"}, True),
    ({"UPI_LITE_LIVE": "true"}, False),
    ({}, False),
])
def test_only_exact_opt_in_is_live(env, live):
    assert ul.Config.from_env(env).live is live


def test_dry_run_still_enforces_limits(tmp_path):
    cfg = ul.Config(live=False, db_path=str(tmp_path / "l.sqlite"))
    assert ul.run_job(job(paise=100_001), cfg)[0] == ul.EXIT_NOT_PAID


# ---- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize("texts,paise,ok", [
    (["₹10"], 1000, True), (["₹ 10.00"], 1000, True), (["10"], 1000, True), (["Rs. 10"], 1000, True),
    (["₹1,000"], 100_000, True), (["₹12.50"], 1250, True), (["₹12.5"], 1250, True), (["₹12.05"], 1205, True),
    (["₹100"], 1000, False), (["₹10.50"], 1000, False), (["₹110"], 1000, False), (["Order 10 items"], 1000, False),
    (["₹12.05"], 1250, False), ([], 1000, False),
])
def test_amount_visible(texts, paise, ok):
    assert ul.amount_visible(texts, paise) is ok


@pytest.mark.parametrize("qr,expected", [
    ("upi://pay?pa=shop@ybl&pn=Shop", "upi://pay?pa=shop@ybl&pn=Shop&am=10.00"),
    ("upi://pay?pa=shop@ybl&am=&tr=T1", "upi://pay?pa=shop@ybl&tr=T1&am=10.00"),
    ("upi://pay?pa=shop@ybl&am=10.00&tr=T1&sign=abc", "upi://pay?pa=shop@ybl&am=10.00&tr=T1&sign=abc"),
    ("upi://pay?pa=shop%40ybl&mc=5411", "upi://pay?pa=shop%40ybl&mc=5411&am=10.00"),
])
def test_raw_qr_is_kept_and_only_gains_a_missing_amount(qr, expected):
    assert ul.build_upi_uri(upi_id="shop@ybl", payee_name=None, paise=1000, qr_text=qr) == expected


def test_raw_qr_for_another_payee_is_refused():
    with pytest.raises(ul.NotPaid):
        ul.build_upi_uri(upi_id="shop@ybl", payee_name=None, paise=1000, qr_text="upi://pay?pa=evil@ybl")


def test_generated_uri_encodes_the_name():
    assert ul.build_upi_uri(upi_id="a@ybl", payee_name="A & B", paise=150, qr_text=None) == \
        "upi://pay?pa=a@ybl&pn=A%20%26%20B&am=1.50&cu=INR&tn=RupeeRail"


def test_result_text_already_on_the_pay_screen_does_not_count():
    before = ["Paid", "₹10", "chaipoint@okaxis"]  # chat history with this merchant
    assert ul.classify_result(before, before, 1000)[0] == "unknown"


def test_resource_ids_do_not_count_as_screen_text():
    assert ul.parse_ui_dump(dump(["Hello"])) == ["Hello"]  # the fake's resource-id says "failed"
    assert ul.parse_ui_dump("ERROR: null root node returned") is None
    assert ul.parse_ui_dump("<hierarchy><broken") is None


def test_pin_prompt_means_no_money_moved():
    assert ul.classify_result(PAY_SCREEN, ["Enter your UPI PIN"], 1000)[0] == "failed"


# ---- rehearsal never pays -----------------------------------------------------------------------


def test_rehearse_never_taps_pay_and_cleans_up(cfg, capsys):
    p = FakePhone()
    cfg.delays = {k: 0 for k in cfg.delays}
    assert ul.cmd_rehearse(cfg, p, VPA, "10", "Chai Point", sleep=lambda _: None) == 0
    assert not p.pay_tapped and len(p.taps) == 4 and "PASS" in capsys.readouterr().out
    assert any("rm -f" in " ".join(c) for c in p.calls[-3:])


def test_rehearse_reports_a_bad_screen(cfg, capsys):
    cfg.delays = {k: 0 for k in cfg.delays}
    cfg.verify_wait_sec = 0
    p = FakePhone(pay_screen=["other@ybl", "₹10", "UPI Lite"])
    assert ul.cmd_rehearse(cfg, p, VPA, "10", "Chai Point", sleep=lambda _: None) == 1
    assert not p.pay_tapped and "FAIL" in capsys.readouterr().out


# ---- the real Adb class against a stand-in binary ------------------------------------------------


FAKE_ADB = r"""#!/bin/sh
echo "$*" >> "$FAKE_ADB_LOG"
case "$*" in
  *dumpsys*) echo "mCurrentFocus=Window{1 u0 com.google.android.apps.nbu.paisa.user/.Main}" ;;
  *"input tap 572 2054"*) touch "$FAKE_ADB_LOG.paid" ;;
  *"input tap 871 2196"*) touch "$FAKE_ADB_LOG.done" ;;
  *"shell cat"*)
    if [ -f "$FAKE_ADB_LOG.paid" ]; then
      echo '<hierarchy><node text="Paid"/><node text="₹10.00"/></hierarchy>'
    elif [ ! -f "$FAKE_ADB_LOG.done" ]; then
      printf '<hierarchy><node text="Scan any&#10;QR code"/></hierarchy>'
    else
      echo '<hierarchy><node text="chaipoint@okaxis"/><node text="₹10"/><node text="UPI Lite"/></hierarchy>'
    fi ;;
esac
"""


def test_real_adb_class_and_real_qr_file(cfg, tmp_path, monkeypatch):
    adb = tmp_path / "adb"
    adb.write_text(FAKE_ADB)
    adb.chmod(adb.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "adb.log"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    cfg.delays = {k: 0 for k in cfg.delays}
    code, res = ul.run_job(job(), cfg, ul.Adb(str(adb)))
    assert code == ul.EXIT_PAID and res["status"] == "paid"
    calls = log.read_text()
    assert "push " in calls and calls.index("uiautomator") < calls.index("input tap 572 2054")
