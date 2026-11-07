/**
 * What the publisher may be asked to do, and how each request becomes one atomic Arkiv batch.
 *
 * validateOp is the privacy gate: anything it doesn't name is refused, so a prompt or a profile can't reach Arkiv by
 * accident. planOp is pure. It returns null when the current state already reflects the request, which also makes
 * every request safe to check again after an uncertain send.
 */

import { ExpirationTime } from "@arkiv-network/sdk";
import { addr, u64 } from "@arkiv-network/sdk/schema";
import { AgentCard, CARD_DAYS, LIVE_MINUTES, LiveRun, Receipt, RECEIPT_DAYS } from "./schema.ts";

export const CATEGORIES = ["food", "groceries", "shopping", "travel", "bills", "other"] as const;
export const OUTCOMES = ["paid", "not_paid", "unknown"] as const;
// In step with AVATARS and COLOURS in agent-server/app/templates.py, which a test there checks.
export const AVATARS = ["chef", "shopper", "traveller", "gardener", "student", "doctor", "artist", "musician", "athlete", "mechanic", "scientist", "robot"] as const;
export const COLOURS = ["saffron", "coral", "rose", "violet", "indigo", "teal", "leaf", "slate"] as const;
const MAX_DELETES = 50;

export interface CardFields { wallet: string; category: string; name: string; avatar: string; colour: string }
export interface ReceiptFields { offrampId: string; outcome: string; inrPaise: number; finishedAt: number; chain: string; lockTx: string }

export type Op =
  | { id: string; kind: "card"; card: CardFields }
  | { id: string; kind: "run_started"; card: CardFields; startedAt: number }
  | { id: string; kind: "run_ended"; wallet: string }
  | { id: string; kind: "run_finished"; card: CardFields; receipt: ReceiptFields }
  | { id: string; kind: "withdraw"; wallet: string; receipts: boolean };

export class InvalidOp extends Error {}

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const HASH = /^0x[0-9a-fA-F]{64}$/;
const OP_ID = /^[A-Za-z0-9:_.-]{1,120}$/;
const CHAINS = ["arc", "whitechain"];

function fail(message: string): never {
  throw new InvalidOp(message);
}

function only(value: unknown, keys: string[], what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail(`${what} must be an object`);
  const object = value as Record<string, unknown>;
  for (const key of Object.keys(object)) if (!keys.includes(key)) fail(`${what} has a field that can't be published: ${key}`);
  for (const key of keys) if (object[key] === undefined) fail(`${what} is missing ${key}`);
  return object;
}

function positive(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) fail(`${what} must be a positive whole number`);
  return value;
}

function address(value: unknown): string {
  if (typeof value !== "string" || !ADDRESS.test(value)) fail("wallet isn't an address");
  return value;
}

function card(value: unknown): CardFields {
  const c = only(value, ["wallet", "category", "name", "avatar", "colour"], "card");
  if (typeof c.name !== "string" || !c.name.trim() || [...c.name].length > 40) fail("name must be 1 to 40 characters");
  if (!CATEGORIES.includes(c.category as never)) fail("unknown category");
  if (!AVATARS.includes(c.avatar as never)) fail("unknown avatar");
  if (!COLOURS.includes(c.colour as never)) fail("unknown colour");
  return { wallet: address(c.wallet), category: c.category as string, name: c.name, avatar: c.avatar as string, colour: c.colour as string };
}

function receipt(value: unknown): ReceiptFields {
  const r = only(value, ["offrampId", "outcome", "inrPaise", "finishedAt", "chain", "lockTx"], "receipt");
  if (typeof r.offrampId !== "string" || !HASH.test(r.offrampId)) fail("offrampId isn't a hash");
  if (typeof r.lockTx !== "string" || !HASH.test(r.lockTx)) fail("lockTx isn't a hash");
  if (!OUTCOMES.includes(r.outcome as never)) fail("unknown outcome");
  if (!CHAINS.includes(r.chain as string)) fail("unknown chain");
  return { offrampId: r.offrampId, outcome: r.outcome as string, inrPaise: positive(r.inrPaise, "inrPaise"), finishedAt: positive(r.finishedAt, "finishedAt"), chain: r.chain as string, lockTx: r.lockTx };
}

export function validateOp(value: unknown): Op {
  const kind = (value as { kind?: unknown } | null)?.kind;
  const id = (value as { id?: unknown } | null)?.id;
  if (typeof id !== "string" || !OP_ID.test(id)) fail("the op needs an id");
  switch (kind) {
    case "card": {
      const o = only(value, ["id", "kind", "card"], "op");
      return { id, kind, card: card(o.card) };
    }
    case "run_started": {
      const o = only(value, ["id", "kind", "card", "startedAt"], "op");
      return { id, kind, card: card(o.card), startedAt: positive(o.startedAt, "startedAt") };
    }
    case "run_ended": {
      const o = only(value, ["id", "kind", "wallet"], "op");
      return { id, kind, wallet: address(o.wallet) };
    }
    case "run_finished": {
      const o = only(value, ["id", "kind", "card", "receipt"], "op");
      return { id, kind, card: card(o.card), receipt: receipt(o.receipt) };
    }
    case "withdraw": {
      const o = only(value, ["id", "kind", "wallet", "receipts"], "op");
      if (typeof o.receipts !== "boolean") fail("receipts must be true or false");
      return { id, kind, wallet: address(o.wallet), receipts: o.receipts };
    }
    default:
      return fail("unknown kind");
  }
}

export function opWallet(op: Op): string {
  return "card" in op ? op.card.wallet : op.wallet;
}

/** What's on Arkiv now for one wallet, as far as this request cares. */
export interface WalletState {
  card: (CardFields & { key: `0x${string}` }) | null;
  lives: { key: `0x${string}`; startedAt: number }[];
  receipts: { key: `0x${string}` }[];
  receiptExists: boolean; // the op's own receipt
}

export interface Plan {
  creates: unknown[];
  patches: unknown[];
  deletes: { entityKey: `0x${string}` }[];
  extensions: { entityKey: `0x${string}`; expiration: unknown }[];
}

function emptyPlan(): Plan {
  return { creates: [], patches: [], deletes: [], extensions: [] };
}

function cardCreate(c: CardFields) {
  return {
    schema: AgentCard,
    values: { wallet: addr(c.wallet as `0x${string}`), category: c.category, name: c.name, avatar: c.avatar, colour: c.colour },
    expiration: ExpirationTime.fromDays(CARD_DAYS),
    flags: { readonly: false, permissionlessExtension: false },
  };
}

/** Creates the card, or patches it if its public details have changed. */
function ensureCard(plan: Plan, c: CardFields, state: WalletState): void {
  if (!state.card) {
    plan.creates.push(cardCreate(c));
    return;
  }
  const set: Record<string, string> = {};
  for (const field of ["category", "name", "avatar", "colour"] as const) if (state.card[field] !== c[field]) set[field] = c[field];
  if (Object.keys(set).length) plan.patches.push({ schema: AgentCard, entityKey: state.card.key, set });
}

function isEmpty(plan: Plan): boolean {
  return !plan.creates.length && !plan.patches.length && !plan.deletes.length && !plan.extensions.length;
}

export function operationCount(plan: Plan): number {
  return plan.creates.length + plan.patches.length + plan.deletes.length + plan.extensions.length;
}

export function planOp(op: Op, state: WalletState): Plan | null {
  const plan = emptyPlan();
  switch (op.kind) {
    case "card":
      ensureCard(plan, op.card, state);
      break;
    case "run_started":
      if (state.lives.some((live) => live.startedAt === op.startedAt)) return null;
      ensureCard(plan, op.card, state);
      plan.deletes.push(...state.lives.map((live) => ({ entityKey: live.key })));
      plan.creates.push({
        schema: LiveRun,
        values: { wallet: addr(op.card.wallet as `0x${string}`), startedAt: u64(BigInt(op.startedAt)) },
        expiration: ExpirationTime.fromMinutes(LIVE_MINUTES),
        flags: { readonly: true, permissionlessExtension: false },
      });
      break;
    case "run_ended":
      plan.deletes.push(...state.lives.map((live) => ({ entityKey: live.key })));
      break;
    case "run_finished": {
      if (state.receiptExists) return null;
      const r = op.receipt;
      plan.creates.push({
        schema: Receipt,
        values: {
          wallet: addr(op.card.wallet as `0x${string}`), offrampId: r.offrampId, outcome: r.outcome, inrPaise: u64(BigInt(r.inrPaise)),
          finishedAt: u64(BigInt(r.finishedAt)), chain: r.chain, lockTx: r.lockTx,
        },
        expiration: ExpirationTime.fromDays(RECEIPT_DAYS),
        flags: { readonly: true, permissionlessExtension: false },
        salt: BigInt(r.offrampId.slice(0, 34)), // the first 128 bits
      });
      ensureCard(plan, op.card, state);
      plan.deletes.push(...state.lives.map((live) => ({ entityKey: live.key })));
      // One atomic batch: the receipt appears, the agent stops showing as working, and a paid run renews its listing.
      if (r.outcome === "paid" && state.card) plan.extensions.push({ entityKey: state.card.key, expiration: ExpirationTime.fromDays(CARD_DAYS) });
      break;
    }
    case "withdraw":
      if (state.card) plan.deletes.push({ entityKey: state.card.key });
      plan.deletes.push(...state.lives.map((live) => ({ entityKey: live.key })));
      if (op.receipts) plan.deletes.push(...state.receipts.map((r) => ({ entityKey: r.key })));
      plan.deletes = plan.deletes.slice(0, MAX_DELETES);
      break;
  }
  return isEmpty(plan) ? null : plan;
}
