/**
 * Applies requests one at a time, following Arkiv's write-safety guidance: an unsigned estimate, explicit nonce, gas and
 * gas price, a durable journal entry before signing, a receipt check and a read-back.
 *
 * Nothing is ever resent automatically. If a send's outcome is uncertain, the current state decides: if the request's
 * effect is on Arkiv it's done, and otherwise every later write stops until a person runs `npm run reconcile`.
 */

import { closeSync, fsyncSync, mkdirSync, openSync, readFileSync, writeSync } from "node:fs";
import { dirname } from "node:path";
import { eq } from "@arkiv-network/sdk/query";
import { addr } from "@arkiv-network/sdk/schema";
import { operationCount, opWallet, planOp, type Op, type Plan, type WalletState } from "./ops.ts";
import { AgentCard, LiveRun, Receipt } from "./schema.ts";

const PAGE = 100;

export class NeedsReconcile extends Error {
  opId: string;

  constructor(opId: string) {
    super(`request ${opId} has an uncertain outcome; run npm run reconcile`);
    this.opId = opId;
  }
}

export interface JournalEntry { at?: string; opId: string; state: "intent" | "sent" | "verified" | "uncertain" | "abandoned"; op?: Op; txHash?: string; nonce?: number; gas?: string; gasPrice?: string }

export class Journal {
  path: string;

  constructor(path: string) {
    this.path = path;
    mkdirSync(dirname(path), { recursive: true });
  }

  /** Reaches the disk before returning, so a crash can't lead to a blind resend. */
  add(entry: JournalEntry): void {
    const fd = openSync(this.path, "a", 0o600);
    try {
      writeSync(fd, `${JSON.stringify({ at: new Date().toISOString(), ...entry })}\n`);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
  }

  entries(): JournalEntry[] {
    try {
      return readFileSync(this.path, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line) as JournalEntry);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error;
    }
  }

  /** Requests whose last known step isn't final. */
  unresolved(): JournalEntry[] {
    const latest = new Map<string, JournalEntry>();
    const ops = new Map<string, Op>();
    for (const entry of this.entries()) {
      latest.set(entry.opId, entry);
      if (entry.op) ops.set(entry.opId, entry.op);
    }
    return [...latest.values()].filter((e) => e.state !== "verified" && e.state !== "abandoned").map((e) => ({ ...e, op: ops.get(e.opId) }));
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

export interface PublisherDeps {
  reader: Any;
  wallet: Any;
  address: `0x${string}`;
  journal: Journal;
  /** Runs the exact batch unsigned and returns its gas estimate, or throws. */
  estimate: (batch: Plan) => Promise<bigint>;
  /** Explicit transaction parameters, or throws if it's over budget or something is pending. */
  txParams: (estimate: bigint, operations: number) => Promise<Any>;
}

export async function readState(reader: Any, publisher: string, op: Op): Promise<WalletState> {
  const wallet = eq("wallet", addr(opWallet(op) as `0x${string}`));
  const cards = await reader.select({ $key: true, wallet: true, category: true, name: true, avatar: true, colour: true }).from(AgentCard).createdBy(publisher).where(wallet).limit(1).fetch();
  const lives = await reader.select({ $key: true, startedAt: true }).from(LiveRun).createdBy(publisher).where(wallet).limit(PAGE).fetch();
  const row = cards.entities[0];
  const state: WalletState = {
    card: row ? { key: row.$key, wallet: row.wallet.value, category: row.category.value, name: row.name.value, avatar: row.avatar.value, colour: row.colour.value } : null,
    lives: lives.entities.map((l: Any) => ({ key: l.$key, startedAt: Number(l.startedAt.value) })),
    receipts: [],
    receiptExists: false,
  };
  if (op.kind === "run_finished") {
    const found = await reader.select({ $key: true }).from(Receipt).createdBy(publisher).where(eq("offrampId", op.receipt.offrampId)).limit(1).fetch();
    state.receiptExists = found.entities.length > 0;
  }
  if (op.kind === "withdraw" && op.receipts) {
    const found = await reader.select({ $key: true }).from(Receipt).createdBy(publisher).where(wallet).limit(PAGE).fetch();
    state.receipts = found.entities.map((r: Any) => ({ key: r.$key }));
  }
  return state;
}

export class Publisher {
  private tail: Promise<unknown> = Promise.resolve();
  private deps: PublisherDeps;

  constructor(deps: PublisherDeps) {
    this.deps = deps;
  }

  /** One write at a time: there's one signer, and its nonces must not collide. */
  apply(op: Op): Promise<{ state: "done"; txHash?: string; alreadyApplied?: boolean }> {
    const result = this.tail.then(() => this.applyNow(op));
    this.tail = result.catch(() => undefined);
    return result;
  }

  private async reconcile(): Promise<void> {
    const { reader, address, journal } = this.deps;
    for (const entry of journal.unresolved()) {
      if (!entry.op) throw new NeedsReconcile(entry.opId);
      const plan = planOp(entry.op, await readState(reader, address, entry.op));
      if (plan) throw new NeedsReconcile(entry.opId);
      journal.add({ opId: entry.opId, state: "verified", txHash: entry.txHash });
    }
  }

  private async applyNow(op: Op): Promise<{ state: "done"; txHash?: string; alreadyApplied?: boolean }> {
    const { reader, wallet, address, journal, estimate, txParams } = this.deps;
    await this.reconcile();
    let txHash: string | undefined;
    let alreadyApplied = true;
    // Only a withdrawal can need more than one batch, when an agent has many receipts.
    for (let round = 0; round < 20; round++) {
      const plan = planOp(op, await readState(reader, address, op));
      if (!plan) return { state: "done", txHash, ...(alreadyApplied ? { alreadyApplied } : {}) };
      alreadyApplied = false;
      const gasEstimate = await estimate(plan);
      const params = await txParams(gasEstimate, operationCount(plan));
      journal.add({ opId: op.id, state: "intent", op, nonce: params.nonce, gas: String(params.gas), gasPrice: String(params.gasPrice) });
      try {
        const result = await wallet.executeBatch(plan, params);
        txHash = result.txHash;
        journal.add({ opId: op.id, state: "sent", txHash });
        const receipt = await reader.getTransactionReceipt({ hash: result.txHash });
        if (receipt.status !== "success") throw new Error("reverted");
      } catch (error) {
        const known = (error as { txHash?: string }).txHash;
        if (known) txHash = known;
        // A thrown error doesn't prove nothing happened. Only the state on Arkiv can say that.
        const settled = await readState(reader, address, op).then((state) => !planOp(op, state)).catch(() => false);
        if (settled) {
          journal.add({ opId: op.id, state: "verified", txHash });
          return { state: "done", txHash };
        }
        journal.add({ opId: op.id, state: "uncertain", txHash });
        throw new NeedsReconcile(op.id);
      }
      if (op.kind !== "withdraw") {
        const left = planOp(op, await readState(reader, address, op));
        // The read-back. The transaction is confirmed, so a disagreement stops writes rather than sending again.
        if (left) {
          journal.add({ opId: op.id, state: "uncertain", txHash });
          throw new NeedsReconcile(op.id);
        }
      }
      journal.add({ opId: op.id, state: "verified", txHash });
      if (op.kind !== "withdraw") return { state: "done", txHash };
    }
    throw new NeedsReconcile(op.id);
  }
}
