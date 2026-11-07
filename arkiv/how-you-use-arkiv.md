# How we use Arkiv

Each feature below changes something a person sees. The feature list is deliberately short.

| Feature | How we use it | What people see | Code |
|---|---|---|---|
| Typed, indexed attributes | `inrPaise` and `finishedAt` are `u64`, and `outcome`, `category` and `wallet` are indexed. Names and links aren't indexed, because nothing filters on them | The filters on an agent's receipts and in the directory | [`schema.ts`](../services/arkiv-board/src/schema.ts) |
| Compound queries and ordering | `and(eq, eq, gte, gte, lt)` with `orderBy(desc("finishedAt"))`, paging by `finishedAt` | "Paid, ₹100+, last 7 days", newest first, with Load more | [`reads.ts`](../services/arkiv-board/src/reads.ts) |
| `$creator` | Every read uses `createdBy(PUBLISHER)`, and `count()` measures what's left out | "Only records created by 0xa0B8…Dd8c count", and how many other cards are ignored | [`reads.ts`](../services/arkiv-board/src/reads.ts) |
| Different Entity Expiration per type | Card 30 days, receipt 30 days, live marker 15 minutes | "Working now" clears by itself after a crash, and "Listed until" on each agent | [`schema.ts`](../services/arkiv-board/src/schema.ts) |
| Lifetime Extension | A lease renewed by activity: each paid run extends the card, and there's no timer | "Each paid order renews this listing", and an inactive agent drops off | [`ops.ts`](../services/arkiv-board/src/ops.ts) |
| Creation flags | Receipts are `readonly`, and cards aren't, because people can rename an agent | "A receipt can't be edited" on each agent's page | [`ops.ts`](../services/arkiv-board/src/ops.ts) |
| Atomic batches | The receipt, the removal of the live marker and the extension are one `executeBatch` | A paid order never shows a new receipt while the agent still says "Working now" | [`ops.ts`](../services/arkiv-board/src/ops.ts) |
| `watchEntityEvents` over a WebSocket | The read service holds one socket, ignores other owners' events and tells pages through server-sent events | Receipts appear and "Working now" clears without a refresh. "Live" shows while connected | [`reader-server.ts`](../services/arkiv-board/src/reader-server.ts) |

## Decisions

- **Filtering:** what we filter on lives in typed attributes. There's no payload to parse, because a receipt is small and every value is either filtered on or shown.
- **Who can edit:** one publisher owns everything. Agents' Circle wallets are smart accounts on Arc, so they can't sign Arkiv transactions. The agent's wallet is therefore an attribute, not the owner.
- **Access key:** it stays on the server, so the browser reads through a fixed, read-only service. It answers a set list of questions, isn't a general RPC proxy, and reports an Arkiv failure as a failure, never as an empty list. Anyone who doesn't want to trust that service can run `npm run verify`.
- **Write safety:** an unsigned estimate, explicit nonce, gas and gas price, a journal entry synced to disk before signing, a receipt check, and a read-back. After an uncertain send, the state on Arkiv decides whether it worked. If it can't tell, writes stop until a person checks, and nothing is ever resent automatically.
- **Missed events:** natural expiry sends no event, so pages also refresh every minute. After any watcher error, the service starts again from the head and tells pages to refetch.

## Features we didn't use

Ownership transfer, permissionless extension, encryption and bytes columns. None would change anything a user sees today.
