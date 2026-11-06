# Onramp with Razorpay: production setup on Azure

INR → stablecoins. The user pays a Razorpay Payment Link. Razorpay tells the API in two ways (a browser callback and a webhook). The API confirms the payment with Razorpay's own API, then the settler calls `vault.onramp`, which sends the tokens from the pool to the user's wallet.

## The exact URLs

Host: `https://rupee-rail.indiasouthcentral.cloudapp.azure.com`. Caddy serves the PWA and the API on this one hostname and already sends `/onramp` and `/onramp/*` to the API (`apps/web/deploy/Caddyfile`), so no proxy change is needed.

| What | Value | Where it goes |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://rupee-rail.indiasouthcentral.cloudapp.azure.com` | **hardcoded default** (`config.AZURE_BASE_URL`); leave unset on Azure, override only for ngrok |
| `WEB_APP_URL` | leave unset (defaults to `PUBLIC_BASE_URL`; the PWA is at `/`) | server `.env` |
| Callback URL | `https://rupee-rail.indiasouthcentral.cloudapp.azure.com/onramp/razorpay/callback` | nowhere: the API sends it with every payment link |
| Webhook URL | `https://rupee-rail.indiasouthcentral.cloudapp.azure.com/onramp/razorpay/webhook` | Razorpay Dashboard → Webhooks |
| Where the payer lands | `https://rupee-rail.indiasouthcentral.cloudapp.azure.com/?onramp=<orderId>&payment=received` | automatic (the PWA's Buy tab picks it up) |

An override of `PUBLIC_BASE_URL` must be `https://`, or the server refuses to start. A trailing slash is stripped.

## Server `.env` (repo root on the VM, read as text, never sourced)

```
COLLECT_RAIL=razorpay
RAZORPAY_API_KEY=rzp_test_...        # rzp_live_... once the account is activated
RAZORPAY_API_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...          # the same text you type in the Dashboard webhook
```

`APP_ENV` stays `dev` (the default) until the RazorpayX payout rail exists. `APP_ENV=prod` still refuses to start because `PAYOUT_RAIL=razorpayx` isn't implemented. With `COLLECT_RAIL=razorpay`, a missing key or a non-https `PUBLIC_BASE_URL` override stops startup with a message that names only the variables, never their values.

Use a real random webhook secret on the server (for example `openssl rand -hex 32`), not the `abcdef` used for local testing.

## Razorpay Dashboard (do this once per mode, test and live)

1. **Webhooks → Add new webhook**
   - URL: `https://rupee-rail.indiasouthcentral.cloudapp.azure.com/onramp/razorpay/webhook`
   - Secret: the value of `RAZORPAY_WEBHOOK_SECRET`
   - Events: `payment_link.paid`, `payment_link.expired`, `payment_link.cancelled`
   
   A merchant account can't create webhooks through the API; only Partner accounts can. So this step is manual.
2. **Business name**: the payment page header ("Payment Request from …") is the account's business name, set under Account & Settings. Our links set only the description, `RupeeRail`.
3. **Live mode only**: register the website `https://rupee-rail.indiasouthcentral.cloudapp.azure.com` under Account & Settings → Website and app details (needed for activation), and switch to `rzp_live_` keys.

## Before going live: fund the pool

`POST /onramp` refuses an order the vault can't pay (`POOL_INSUFFICIENT`, `DAILY_CAP`) before any INR is taken. Check, then top up with `fundPool` from the owner wallet:

```
cast call <vault> "poolBalance()(uint256)" --rpc-url <rpc>
cast call <vault> "remainingDailyCap()(uint256)" --rpc-url <rpc>
```

## Deploy order

1. Merge #5 (dev/prod rails), then #6 (Razorpay backend), then #7 (Buy tab).
2. API: pull on the VM, set the `.env` above, restart the API with **workers on**. Workers run the reconcile loop, which retries after Razorpay or chain errors.
3. PWA: `npm run build` and rsync `dist/` (see `apps/web/README.md`). The Caddyfile doesn't change.
4. Add the Dashboard webhook (above).

## Verify after deploying

```
H=https://rupee-rail.indiasouthcentral.cloudapp.azure.com
curl -s $H/health | jq .diagnostics.collectRail                 # "razorpay"
curl -s -X POST $H/onramp/razorpay/webhook -d '{}'              # 400 BAD_SIGNATURE (route live, signature enforced)
curl -s -o /dev/null -w '%{redirect_url}\n' "$H/onramp/razorpay/callback?razorpay_payment_link_id=x"
                                                                # $H/?onramp=&payment=unverified
```

Then a test-mode purchase in the PWA: Buy → ₹10 → Pay with Razorpay → UPI `success@razorpay`. Expect:
- you land back on the Buy tab, which shows "… USDC sent";
- the server log shows the callback (303), the webhook (200), and `sent onramp`;
- `cast call <vault> "onrampDone(bytes32)(bool)" <orderId>` returns `true`.

Resending the webhook from the Dashboard must not send a second transaction.

## What runs where

```
PWA (Buy tab) ──POST /onramp──► API: price at FX ask + spread, check pool + daily cap, create Payment Link
PWA ──full-page──► Razorpay hosted page ──user pays──►
  Razorpay ──browser 303──► /onramp/razorpay/callback   check HMAC(API secret), record, redirect to /?onramp=<id>
  Razorpay ──server POST──► /onramp/razorpay/webhook    check HMAC(webhook secret), dedupe by event id, record
both ──► confirm: GET /v1/payment_links/{id} must say paid, full amount, INR, this order
      ──► settler: vault.onramp(orderId, wallet, amount, inrPaise) ──► OnrampPaid, order PAID
PWA polls GET /onramp/{id} ──► "0.099 USDC sent" + explorer link
```

Neither signal is trusted with money on its own: only Razorpay's API answer is. Payout happens once per order, enforced three ways: a per-order lock, the DB compare-and-set, and `onrampDone(orderId)` onchain.

## Known gaps

- **Refunds**: a paid order that can't be paid out (amount mismatch) stays `AWAITING_FIAT` with `lastError`. There is no endpoint yet to refund it through Razorpay's refund API.
- **Per-user daily limit**: `DAILY_USER_INR_PAISE` is enforced for offramp quotes only, not for onramp orders.
- **Rate risk**: the rate is fixed when the link is created and held for up to ~16 minutes.
- **Pool monitoring**: nothing alerts when `poolBalance` runs low.
- **KYC / compliance** for selling crypto to INR payers isn't handled by the app.
