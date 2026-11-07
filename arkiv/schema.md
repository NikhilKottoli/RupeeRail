# Arkiv schema

RupeeRail's agents shop and pay from their own wallets. Each published agent has a public track record on Rabdi (chain 7794095). The code is in `services/arkiv-board/src/schema.ts`, and every query is in `reads.ts`.

| Entity | Owner | Expires | Why that lifetime | Who can edit |
|---|---|---|---|---|
| `rupeerail.agent` | Publisher wallet | 30 days, renewed by each paid run | An agent that stops paying drops off the directory by itself | Publisher can patch it, because a person can rename an agent |
| `rupeerail.receipt` | Publisher wallet | 30 days | Outlasts the 24-hour dispute window and the 7-day dispute limit | Nobody: created read-only |
| `rupeerail.live` | Publisher wallet | 15 minutes | Longer than the longest run (a 10-minute limit plus a 2-minute wait for the result), so a crash can't leave an agent showing "Working now" | Nobody: created read-only, and deleted when the run ends |

**Writer wallet:** [`0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c`](https://rabdi.explorer.arkiv.network/address/0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c), which creates and owns every entity. Only the publisher service on the machine running the agents holds its key.

## Fields

Indexed fields are the ones queries filter on. Amounts and times are `u64`, so they can be compared as ranges.

**`rupeerail.agent`**

| Field | Type | Indexed | Used for |
|---|---|---|---|
| `wallet` | `addr` | Yes | Finding one agent's card |
| `category` | `str` | Yes | The directory's category filter |
| `name`, `avatar`, `colour` | `str` | No | Display. `avatar` and `colour` are ids from a fixed list |

**`rupeerail.receipt`**

| Field | Type | Indexed | Used for |
|---|---|---|---|
| `wallet` | `addr` | Yes | One agent's receipts |
| `offrampId` | `str` | Yes | Refusing a duplicate, and linking to the vault's record |
| `outcome` | `str` | Yes | `paid`, `not_paid` or `unknown` |
| `inrPaise` | `u64` | Yes | The amount filter |
| `finishedAt` | `u64` | Yes | The time filter, newest-first order and paging. Milliseconds |
| `chain`, `lockTx` | `str` | No | The link to the lock on Arc |

**`rupeerail.live`**: `wallet` `addr` (indexed), `startedAt` `u64`.

## The queries the app runs

Every query filters on the on-chain creator, because anyone can write an entity with these schema names.

```ts
// The directory, optionally by category
select(fields).from(AgentCard).createdBy(PUBLISHER).where(eq("category", "food")).orderBy(asc("$key")).limit(100)

// One agent's card
select(fields).from(AgentCard).createdBy(PUBLISHER).where(eq("wallet", addr(wallet))).limit(1)

// Receipts: wallet, plus any of outcome, amount and time, newest first. Paged by finishedAt
select(fields).from(Receipt).createdBy(PUBLISHER)
  .where(and(eq("wallet", addr(wallet)), eq("outcome", "paid"), gte("inrPaise", u64(10000n)), gte("finishedAt", u64(since))))
  .orderBy(desc("finishedAt")).limit(20)

// Who is working now
select({ wallet: true }).from(LiveRun).createdBy(PUBLISHER).limit(100)

// Cards from other wallets, which the app ignores and counts on the page
count().from(AgentCard)  // minus count().from(AgentCard).createdBy(PUBLISHER)
```

`services/arkiv-board/src/verify.ts` runs these with your own access key, without any RupeeRail server.

## Writes

Each finished run is one atomic `executeBatch`:

| Run | Creates | Deletes | Extends |
|---|---|---|---|
| Paid | Receipt | The `live` marker | The card, for another 30 days |
| Locked funds, not paid | Receipt | The `live` marker | Nothing |
| Never locked funds | Nothing | The `live` marker | Nothing |

The receipt, the end of "Working now" and the renewal therefore land together or not at all. Each write is journaled before it's signed and is never resent automatically. See `services/arkiv-board/src/publish.ts`.

## What stays off Arkiv

Publishing is off for each agent until a person turns it on. These never go on Arkiv, and the publisher refuses any field it doesn't know, so they can't reach it by accident:

- Prompts, instructions and profiles, such as diets and addresses.
- Which sites an agent shops on, and what it bought.
- UPI IDs, payee names, raw QR text and UTRs, even hashed, because a 12-digit UTR can be brute-forced.
- The commitment salt. With it, anyone could check guesses at a payee against the on-chain commitment.
- Keys and access tokens.

A wallet, offramp ID, lock transaction and token amount are already public on Arc. Publishing does add one link that wasn't public: an agent's name beside its wallet.

## Trust

- Readers filter on `$creator`, never on a field anyone can write. A card from another wallet is ignored, and the page says how many there are.
- A receipt can't be edited, but its owner can still delete it, as anyone turning off publishing does. Read-only doesn't mean permanent.
- A receipt is the publisher's own claim. The lock on Arc can be checked independently, but no chain can confirm that a UPI payment arrived, so `paid` means the phone that paid reported it.
