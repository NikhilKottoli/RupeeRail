# Arkiv schema (draft)

RupeeRail's agent payout board, on the Rabdi testnet. Agents post, claim and settle UPI payout jobs, and any app can read the board without asking us. **Draft: nothing has been written yet.**

| Entity | Owner | Expires | Why that lifetime |
|---|---|---|---|
| `rupeerail.job` | Board publisher wallet | 10 minutes after the lock | Payers refuse locks older than 10 minutes, so an expired job can't be paid |
| `rupeerail.claim` | The payer's wallet | 2 minutes | A payment takes about 30 seconds, so a stale claim clears itself |
| `rupeerail.receipt` | The payer's wallet | 30 days | Outlives the 24-hour dispute window, for history |
| `rupeerail.payer` | The payer's wallet | 2 minutes, extended by a heartbeat | The entity expiring is what marks a payer offline |

## Fields

Indexed fields are the ones queries filter on.

**`rupeerail.job`**

| Field | Type | Indexed | Notes |
|---|---|---|---|
| `offrampId` | `str` | Yes | The vault's id, already public on-chain |
| `chain` | `str` | Yes | `arc` or `whitechain` |
| `inrPaise` | `u64` | Yes | Lets a payer filter by what it can afford |
| `user` | `addr` | Yes | The paying wallet, already public on-chain |
| `lockedAt` | `u64` | Yes | |
| `amount` | `str` | No | Token base units |

**`rupeerail.claim`**: `offrampId` (indexed), `payer` `addr` (indexed). Read-only once created.

**`rupeerail.receipt`**: `offrampId` (indexed), `payer` `addr` (indexed), `outcome` `str` (indexed: `paid`, `failed` or `unknown`), `finishedAt` `u64` (indexed), `inrPaise` `u64`. Read-only once created.

**`rupeerail.payer`**: `payer` `addr` (indexed), `maxInrPaise` `u64` (indexed), `chains` `str`, `status` `str` (indexed: `ready` or `busy`).

## What stays off Arkiv

Arkiv entities are public, so these never go on it:

- UPI IDs, payee names and raw QR text.
- The commitment salt. With it, anyone could check guesses at a payee against the on-chain commitment.
- UTRs, even hashed. They're 12 digits, so a hash can be brute-forced.
- Keys and access tokens.

Every field above is either already public on-chain or says nothing about who was paid.

## Trust

Anyone can create an entity with any of these schema names, so nothing is trusted by name alone. Readers filter on `$owner` and `$creator`:

- Jobs count only from the board publisher's wallet.
- Claims and receipts count only from a known payer wallet.
- For competing claims, the earliest one wins.

A receipt is still the payer's own claim. It's more honest than today's `COMPLETED`, because the wallet that paid signs it, but the chain can't verify a UPI payment.

## What users see

- **Status:** the PWA shows "Merchant paid" only for a receipt with outcome `paid` from a known payer, instead of the current "Payment processed". A `failed` or `unknown` receipt points the user to a dispute.
- **Availability:** a live `rupeerail.payer` entity shows payers as online. If none exist, the app warns before the user pays.
- **Board:** a public screen shows open jobs and active payers.

The PWA reads all of this through a read proxy on our server, because Arkiv access keys must not ship in a browser bundle. Writes happen only from the board publisher and the payer, each with its own wallet.

## Example query (once live)

```
arkiv.schema = str('rupeerail.job') AND inrPaise <= u64(100000)
```
