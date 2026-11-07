import assert from "node:assert/strict";
import { test } from "node:test";
import { InvalidOp, operationCount, planOp, validateOp, type Op, type WalletState } from "../src/ops.ts";

const WALLET = `0x${"a".repeat(40)}`;
const HASH = `0x${"b".repeat(64)}`;
const KEY = `0x${"c".repeat(64)}` as `0x${string}`;
const LIVE = `0x${"d".repeat(64)}` as `0x${string}`;
const card = { wallet: WALLET, category: "food", name: "Butler", avatar: "chef", colour: "saffron" };
const receipt = { offrampId: HASH, outcome: "paid", inrPaise: 23200, finishedAt: 1_794_000_000_000, chain: "arc", lockTx: HASH };
const finished: Op = { id: "r1", kind: "run_finished", card, receipt };
const nothing: WalletState = { card: null, lives: [], receipts: [], receiptExists: false };
const listed: WalletState = { ...nothing, card: { ...card, key: KEY } };

test("only named fields can be published", () => {
  for (const extra of ["prompt", "profile", "site", "instructions", "payee"]) {
    assert.throws(() => validateOp({ id: "x", kind: "card", card: { ...card, [extra]: "secret" } }), InvalidOp);
    assert.throws(() => validateOp({ id: "x", kind: "run_finished", card, receipt: { ...receipt, [extra]: "secret" } }), InvalidOp);
    assert.throws(() => validateOp({ id: "x", kind: "run_ended", wallet: WALLET, [extra]: "secret" }), InvalidOp);
  }
});

test("bad values are refused", () => {
  const bad: unknown[] = [
    null,
    { kind: "card", card },
    { id: "x", kind: "unknown" },
    { id: "x", kind: "card", card: { ...card, avatar: "../evil" } },
    { id: "x", kind: "card", card: { ...card, colour: "red" } },
    { id: "x", kind: "card", card: { ...card, category: "secret" } },
    { id: "x", kind: "card", card: { ...card, name: "" } },
    { id: "x", kind: "card", card: { ...card, name: "n".repeat(41) } },
    { id: "x", kind: "card", card: { ...card, wallet: "not-an-address" } },
    { id: "x", kind: "run_finished", card, receipt: { ...receipt, outcome: "maybe" } },
    { id: "x", kind: "run_finished", card, receipt: { ...receipt, inrPaise: 1.5 } },
    { id: "x", kind: "run_finished", card, receipt: { ...receipt, lockTx: "0x1" } },
  ];
  for (const op of bad) assert.throws(() => validateOp(op), InvalidOp);
});

test("a valid op comes back with only its known fields", () => {
  assert.deepEqual(validateOp(finished), finished);
});

test("a paid run lands its receipt, clears working and renews the listing in one batch", () => {
  const plan = planOp(finished as Op, { ...listed, lives: [{ key: LIVE, startedAt: 1 }] })!;
  assert.equal(plan.creates.length, 1);
  assert.deepEqual(plan.deletes, [{ entityKey: LIVE }]);
  assert.equal(plan.extensions.length, 1);
  assert.equal(plan.extensions[0].entityKey, KEY);
  assert.equal(operationCount(plan), 3);
});

test("a run that didn't pay doesn't renew the listing", () => {
  const op: Op = { id: "r", kind: "run_finished", card, receipt: { ...receipt, outcome: "not_paid" } };
  assert.equal(planOp(op, listed)!.extensions.length, 0);
});

test("the first receipt also creates the card", () => {
  const plan = planOp(finished, nothing)!;
  assert.equal(plan.creates.length, 2);
  assert.equal(plan.extensions.length, 0);
});

test("a receipt is read-only and its key follows from the offramp", () => {
  const create = planOp(finished, listed)!.creates[0] as { flags: { readonly: boolean }; salt: bigint };
  assert.equal(create.flags.readonly, true);
  assert.equal(create.salt, BigInt(HASH.slice(0, 34)));
});

test("a request that has already taken effect plans nothing, so it can't be applied twice", () => {
  assert.equal(planOp(finished, { ...listed, receiptExists: true }), null);
  assert.equal(planOp({ id: "c", kind: "card", card }, listed), null);
  assert.equal(planOp({ id: "s", kind: "run_started", card, startedAt: 5 }, { ...listed, lives: [{ key: LIVE, startedAt: 5 }] }), null);
  assert.equal(planOp({ id: "e", kind: "run_ended", wallet: WALLET }, listed), null);
  assert.equal(planOp({ id: "w", kind: "withdraw", wallet: WALLET, receipts: true }, nothing), null);
});

test("a changed name patches only that field", () => {
  const plan = planOp({ id: "c", kind: "card", card: { ...card, name: "Chef" } }, listed)!;
  assert.deepEqual((plan.patches[0] as { set: unknown }).set, { name: "Chef" });
  assert.equal(plan.creates.length, 0);
});

test("a new run replaces a stale working marker", () => {
  const plan = planOp({ id: "s", kind: "run_started", card, startedAt: 9 }, { ...listed, lives: [{ key: LIVE, startedAt: 1 }] })!;
  assert.deepEqual(plan.deletes, [{ entityKey: LIVE }]);
  assert.equal(plan.creates.length, 1);
});

test("withdrawing removes receipts only when asked", () => {
  const state: WalletState = { ...listed, receipts: [{ key: HASH as `0x${string}` }] };
  assert.equal(planOp({ id: "w", kind: "withdraw", wallet: WALLET, receipts: false }, state)!.deletes.length, 1);
  assert.equal(planOp({ id: "w", kind: "withdraw", wallet: WALLET, receipts: true }, state)!.deletes.length, 2);
});
