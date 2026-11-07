# Why Arkiv

**An agent that spends money needs a track record its operator can't quietly rewrite, and that others can read without asking.** Our agents order and pay on their own, so the useful question before trusting one is what it has done.

## What users would lose on Postgres, or our own API

- **Reading without permission.** Today, anyone can run the exact queries behind our page with their own free key: `npm run verify` in `services/arkiv-board`. On Postgres, they'd get whatever our API chose to return, for as long as we kept running it.
- **Knowing who wrote it.** Each entity's `$creator` is set by the chain. On our own API, "published by RupeeRail" is just text we return. Here, a stranger can write a card with our schema name, and it's still recognisably not ours. Our page counts and ignores those.
- **A record that can't be edited after the fact.** Receipts are created read-only, so a failed payment can't be changed into a paid one, even by us. A database administrator could change a row without anyone noticing. Failures stay visible on the page.
- **Records that clean up after themselves.** Each lifetime follows the product. A crashed run's "Working now" marker vanishes on its own in 15 minutes, and an agent that stops paying drops off the directory after 30 days. On Postgres, these would need a cron job that someone has to keep running.

## Why not the others

- **IPFS:** content can't be filtered. Our page asks for "this wallet's paid receipts over ₹100 in the last 7 days".
- **A subgraph:** it could index what's on Arc, but outcomes, agent names and categories aren't on Arc. They would still need somewhere public to live.
- **Arc itself:** storing and filtering this in a contract would mean a contract change and redeployment, since the vaults aren't upgradeable.

## What we kept off Arkiv, and why

Prompts, instructions, profiles such as diets and addresses, sites, purchases, UPI IDs, payee names, QR text, UTRs and salts. Arkiv entities are public and these are personal, so they stay in a private local file with mode 600. The publisher refuses any field it doesn't know, so a bug can't send them by accident. Publishing is off for each agent until a person turns it on.

## What this doesn't solve

We publish these records ourselves from one wallet, so they're our claim, signed and timestamped. Each receipt links to its lock on Arc, which anyone can check independently, but no chain can confirm that a UPI payment arrived. The owner can also delete an entity, and turning off publishing does exactly that. Arkiv makes the record public, attributable and uneditable. It doesn't make it true.
