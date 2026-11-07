/**
 * Every query the product runs. All of them filter on the on-chain creator, because anyone can write an entity with
 * our schema names. Rows come back as plain JSON with bigints as strings.
 */

import { and, asc, desc, eq, gte, lt, lte } from "@arkiv-network/sdk/query";
import { addr, u64 } from "@arkiv-network/sdk/schema";
import { AgentCard, LiveRun, PUBLISHER, Receipt } from "./schema.ts";

const PAGE = 100;
const MAX_RECEIPTS_READ = 1000;
const BLOCK_SEC = 2;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Reader = any;

export interface CardRow { key: string; wallet: string; category: string; name: string; avatar: string; colour: string; creator: string; expiresAtBlock: string; expiresAt: string }
export interface ReceiptRow { key: string; wallet: string; offrampId: string; outcome: string; inrPaise: string; finishedAt: number; chain: string; lockTx: string; creator: string; expiresAt: string }
export interface Stats { paid: number; notPaid: number; unknown: number; paidInrPaise: string; lastPaidAt: number | null }
export interface ReceiptFilter { wallet?: string; outcome?: string; minInrPaise?: number; maxInrPaise?: number; since?: number; before?: number; limit?: number }

function blockToDate(block: bigint, head: bigint, now: number): string {
  return new Date(now + Number(block - head) * BLOCK_SEC * 1000).toISOString();
}

function cardRow(row: Reader, head: bigint, now: number): CardRow {
  return {
    key: row.$key, wallet: row.wallet.value, category: row.category.value, name: row.name.value, avatar: row.avatar.value, colour: row.colour.value,
    creator: row.$creator, expiresAtBlock: String(row.$expiresAt), expiresAt: blockToDate(BigInt(row.$expiresAt), head, now),
  };
}

function receiptRow(row: Reader, head: bigint, now: number): ReceiptRow {
  return {
    key: row.$key, wallet: row.wallet.value, offrampId: row.offrampId.value, outcome: row.outcome.value, inrPaise: String(row.inrPaise.value),
    finishedAt: Number(row.finishedAt.value), chain: row.chain.value, lockTx: row.lockTx.value, creator: row.$creator, expiresAt: blockToDate(BigInt(row.$expiresAt), head, now),
  };
}

const CARD_FIELDS = { $key: true, $creator: true, $expiresAt: true, wallet: true, category: true, name: true, avatar: true, colour: true };
const RECEIPT_FIELDS = { $key: true, $creator: true, $expiresAt: true, wallet: true, offrampId: true, outcome: true, inrPaise: true, finishedAt: true, chain: true, lockTx: true };

export async function listCards(reader: Reader, category?: string, publisher: string = PUBLISHER): Promise<CardRow[]> {
  const head = await reader.getBlockNumber({ cacheTime: 0 });
  const now = Date.now();
  let query = reader.select(CARD_FIELDS).from(AgentCard).createdBy(publisher);
  if (category) query = query.where(eq("category", category));
  const page = await query.orderBy(asc("$key")).limit(PAGE).fetch();
  return page.entities.map((row: Reader) => cardRow(row, head, now));
}

export async function findCard(reader: Reader, wallet: string, publisher: string = PUBLISHER): Promise<CardRow | null> {
  const head = await reader.getBlockNumber({ cacheTime: 0 });
  const page = await reader.select(CARD_FIELDS).from(AgentCard).createdBy(publisher).where(eq("wallet", addr(wallet as `0x${string}`))).limit(1).fetch();
  return page.entities[0] ? cardRow(page.entities[0], head, Date.now()) : null;
}

/** Newest first. To page, pass the last row's finishedAt as `before`. */
export async function listReceipts(reader: Reader, filter: ReceiptFilter, publisher: string = PUBLISHER): Promise<ReceiptRow[]> {
  const head = await reader.getBlockNumber({ cacheTime: 0 });
  const now = Date.now();
  const predicates = [
    ...(filter.wallet ? [eq("wallet", addr(filter.wallet as `0x${string}`))] : []),
    ...(filter.outcome ? [eq("outcome", filter.outcome)] : []),
    ...(filter.minInrPaise !== undefined ? [gte("inrPaise", u64(BigInt(filter.minInrPaise)))] : []),
    ...(filter.maxInrPaise !== undefined ? [lte("inrPaise", u64(BigInt(filter.maxInrPaise)))] : []),
    ...(filter.since !== undefined ? [gte("finishedAt", u64(BigInt(filter.since)))] : []),
    ...(filter.before !== undefined ? [lt("finishedAt", u64(BigInt(filter.before)))] : []),
  ];
  let query = reader.select(RECEIPT_FIELDS).from(Receipt).createdBy(publisher);
  if (predicates.length) query = query.where(predicates.length === 1 ? predicates[0] : and(...predicates));
  const page = await query.orderBy(desc("finishedAt")).limit(Math.min(filter.limit ?? PAGE, PAGE)).fetch();
  return page.entities.map((row: Reader) => receiptRow(row, head, now));
}

export function statsFrom(receipts: ReceiptRow[]): Stats {
  const stats: Stats = { paid: 0, notPaid: 0, unknown: 0, paidInrPaise: "0", lastPaidAt: null };
  let total = 0n;
  for (const r of receipts) {
    if (r.outcome === "paid") {
      stats.paid += 1;
      total += BigInt(r.inrPaise);
      stats.lastPaidAt = Math.max(stats.lastPaidAt ?? 0, r.finishedAt);
    } else if (r.outcome === "not_paid") stats.notPaid += 1;
    else stats.unknown += 1;
  }
  stats.paidInrPaise = String(total);
  return stats;
}

/** Up to MAX_RECEIPTS_READ receipts, paged by finish time. Used for totals, which Arkiv can't sum for us. */
export async function allReceipts(reader: Reader, wallet?: string, publisher: string = PUBLISHER): Promise<ReceiptRow[]> {
  const rows: ReceiptRow[] = [];
  let before: number | undefined;
  while (rows.length < MAX_RECEIPTS_READ) {
    const page = await listReceipts(reader, { wallet, before, limit: PAGE }, publisher);
    rows.push(...page);
    if (page.length < PAGE) break;
    before = page[page.length - 1].finishedAt;
  }
  return rows;
}

export async function workingWallets(reader: Reader, publisher: string = PUBLISHER): Promise<Set<string>> {
  const page = await reader.select({ wallet: true }).from(LiveRun).createdBy(publisher).limit(PAGE).fetch();
  return new Set(page.entities.map((row: Reader) => String(row.wallet.value).toLowerCase()));
}

/** Cards with this schema name from anyone else, which the product ignores and says so. */
export async function foreignCards(reader: Reader, publisher: string = PUBLISHER): Promise<number> {
  const [everyone, ours] = await Promise.all([reader.count().from(AgentCard).fetch(), reader.count().from(AgentCard).createdBy(publisher).fetch()]);
  return Math.max(0, Number(everyone) - Number(ours));
}

/** The queries the product runs, shown to people so they can run them without us. */
export function queryText(publisher: string = PUBLISHER): Record<string, string> {
  return {
    agents: `select().from(AgentCard).createdBy("${publisher}").orderBy(asc("$key"))`,
    receipts: `select().from(Receipt).createdBy("${publisher}").where(and(eq("wallet", addr(wallet)), eq("outcome", "paid"), gte("inrPaise", u64(10000n)))).orderBy(desc("finishedAt"))`,
    working: `select({ wallet: true }).from(LiveRun).createdBy("${publisher}")`,
  };
}
