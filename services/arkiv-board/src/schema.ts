import { addr, defineSchema, str, u64 } from "@arkiv-network/sdk/schema";

/**
 * Everything here is public, and arkiv/schema.md explains each choice. Every field is either already public on Arc, or
 * something the agent's owner chose to publish. Prompts, profiles, instructions, sites, payee names, QR text, salts and
 * UTRs must never be added.
 */

/** An agent's public profile. Owned by the publisher, which can patch it, and kept alive by paid runs. */
export const AgentCard = defineSchema("rupeerail.agent", {
  wallet: addr().indexed(),
  category: str().indexed(),
  name: str(),
  avatar: str(),
  colour: str(),
});

/** One payment attempt that locked funds. Read-only, so it can't be rewritten after the fact. */
export const Receipt = defineSchema("rupeerail.receipt", {
  wallet: addr().indexed(),
  offrampId: str().indexed(),
  outcome: str().indexed(),
  inrPaise: u64().indexed(),
  finishedAt: u64().indexed(),
  chain: str(),
  lockTx: str(),
});

/** An agent working right now. It expires by itself, so a crash can't leave an agent looking busy forever. */
export const LiveRun = defineSchema("rupeerail.live", {
  wallet: addr().indexed(),
  startedAt: u64(),
});

/** The only wallet whose entities count. Anyone can write an entity with these schema names, so readers filter on this. */
export const PUBLISHER = "0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c" as const;

export const CARD_DAYS = 30; // renewed by each paid run, so an agent that stops paying drops off the directory
export const RECEIPT_DAYS = 30; // outlasts the 24-hour dispute window and the 7-day dispute limit
export const LIVE_MINUTES = 15; // longer than the longest run: a 10-minute limit plus a 2-minute wait for the result
