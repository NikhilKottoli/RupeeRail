import { addr, defineSchema, str, u64 } from "@arkiv-network/sdk/schema";

/**
 * Everything here is public. Each field is either already public on-chain or says nothing about who was paid.
 * UPI IDs, payee names, QR text, salts and UTRs must never be added. See arkiv/schema.md.
 */
export const Job = defineSchema("rupeerail.job", {
  offrampId: str().indexed(),
  chain: str().indexed(),
  inrPaise: u64().indexed(),
  user: addr().indexed(),
  lockedAt: u64().indexed(),
  amount: str(),
});
