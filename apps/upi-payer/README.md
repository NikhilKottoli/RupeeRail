# RupeeRail UPI payer

A small app that runs on the machine with the phone attached. It watches for locked offramps on the deployed backend and pays each merchant from Google Pay's UPI Lite balance over ADB. It holds no chain keys, opens no port and needs only `adb` and `segno`.

```
this machine                                             deployed backend
GET /events ------------------------------------------>  OfframpLocked, with the offramp id
GET /offramp/{id}?refresh=true  (x-view-token) ------>  quotes table: upiId and payee name; chain: amount, status
adb --> Google Pay --> merchant receives INR
```

Don't run `services/api-py` here against the live vaults. Its listener would refund any `LOCKED` payment it has no quote for.

## Setup

On the backend, set `VIEW_TOKEN_SECRET` to a long random value and keep `PAYOUT_COMMAND` on the default stub. A real payout provider there would pay the merchant a second time. Set the same secret here: each request's `x-view-token` is `HMAC-SHA256(secret, id)`.

Treat the secret like a password. If it leaks, anyone can read the payee of every offramp quoted while it was set.

Here:

```bash
cd apps/upi-payer
uv sync
cp .env.example .env     # set RUPEERAIL_API_URL and VIEW_TOKEN_SECRET
```

`RUPEERAIL_API_URL` must be `https://`, or `http://` to a private address.

## Calibrate (nothing here can tap Pay)

Connect the phone with USB debugging on, unlock it and leave Google Pay open on its home screen.

```bash
uv run python main.py check    # can't prove VIEW_TOKEN_SECRET; a wrong one logs ALERT at the first payment
uv run python main.py rehearse --upi-id someone@ybl --inr 1 --payee-name "Someone"
uv run python main.py dump-screen
```

`rehearse` walks to the pay screen, prints the text it can read and says whether a live run would tap Pay. It waits 5 s and backs out, so check where the Pay button is. Tap positions and timings are per phone: set `UPI_LITE_TAPS='{"pay":[572,2054]}'` or `UPI_LITE_DELAYS='{"done":4}'`, which merge over the defaults.

## Run

```bash
uv run python main.py run                      # dry run: logs what it would pay
UPI_LITE_LIVE=1 uv run python main.py run      # real money
```

## Limits

| Limit | Default | Change with |
|---|---|---|
| Live payments | Off | `UPI_LITE_LIVE=1` |
| Per payment | ₹1,000 | `UPI_LITE_MAX_INR` |
| Rolling 24 h total | ₹1,000. Paid and unknown payments count | `UPI_LITE_DAILY_CAP_INR` |
| Payees | Any UPI ID | `UPI_LITE_ALLOWLIST=a@ybl,b@paytm` |
| Lock age | Never pays a lock older than 10 minutes | `PAYER_MAX_LOCK_AGE_SEC` |
| Screen check before Pay | `strict`: amount, payee and "UPI Lite" must be readable | `UPI_LITE_VERIFY=strict\|basic\|off` |

These are enforced here, so they hold even if the backend or its token is compromised. `off` taps Pay blindly and could pay a stale QR. Each offramp id pays at most once (ledger in `data/payer.sqlite`).

## Merchant QRs

The backend doesn't return the raw QR, so the phone pays a new QR built from the UPI ID, name and amount. Fields such as `mc`, `tr` and `sign` are lost, so Google Pay may treat a merchant differently or refuse the payment. Rehearse with a real merchant's UPI ID before relying on it.

## Results stay on this machine

The backend isn't told what happened. With the default stub it completes the offramp about 15 s after the lock, whether or not the phone paid. So watch the log:

| Log line | Meaning | What to do |
|---|---|---|
| `PAID` | Success text and the amount were on screen | Nothing |
| `ALERT NOT PAID` | The merchant wasn't paid | Call the backend's `POST /admin/offramp/{id}/reverse` within the 24 h dispute window |
| `ALERT outcome UNKNOWN` | Pay was tapped but the result wasn't clear | Check the phone, then reverse only if it didn't pay |

## Tests

```bash
uv run pytest
```

They use a simulated phone and can't touch a real one.
