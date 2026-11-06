/**
 * Publishes one job for the most recent real lock, then reads it back.
 *
 *   npm run first-job            estimate only, nothing is signed
 *   npm run first-job -- --yes   writes it, costing test GLM
 */

import { ExpirationTime } from "@arkiv-network/sdk";
import { eq } from "@arkiv-network/sdk/query";
import { addr, u64 } from "@arkiv-network/sdk/schema";
import { privateKeyToAccount } from "viem/accounts";
import { estimateWrite, journal, loadEnv, makeClients, RABDI_CHAIN_ID, txParamsFor } from "./arkiv.ts";
import { Job } from "./schema.ts";

const BACKEND = process.env.RUPEERAIL_API_URL ?? "https://rupee-rail.indiasouthcentral.cloudapp.azure.com";
const MAX_COST_WEI = 10n ** 16n; // 0.01 test GLM

async function latestLock() {
  const events = (await (await fetch(`${BACKEND}/events?limit=50`)).json()) as { name: string; id: string; args: Record<string, string> }[];
  const lock = events.find((e) => e.name === "OfframpLocked");
  if (!lock) throw new Error("no locks found on the backend");
  const view = (await (await fetch(`${BACKEND}/offramp/${lock.id}?refresh=true`)).json()) as { chain: string; user: string; amount: string; inrPaise: string; onchain: { lockedAt: string } };
  return { offrampId: lock.id, chain: view.chain, user: view.user, amount: view.amount, inrPaise: BigInt(view.inrPaise), lockedAt: BigInt(view.onchain.lockedAt) };
}

async function main() {
  const send = process.argv.includes("--yes");
  const env = loadEnv();
  const account = privateKeyToAccount(env.BOARD_PUBLISHER_PRIVATE_KEY as `0x${string}`);
  const accessKey = env.ARKIV_ACCESS_KEY;
  const { reader, wallet } = makeClients(account, accessKey);
  if ((await reader.getChainId()) !== RABDI_CHAIN_ID) throw new Error("wrong chain");

  const lock = await latestLock();
  console.log(`latest lock: ${lock.offrampId.slice(0, 12)}… on ${lock.chain}, ${lock.inrPaise} paise`);

  const existing = await reader.select({ $key: true }).from(Job, { onMismatch: "throw" }).createdBy(account.address)
    .where(eq("offrampId", lock.offrampId)).limit(1).fetch();
  if (existing.entities[0]) {
    console.log(`already published: ${existing.entities[0].$key}`);
    return;
  }

  const values = {
    offrampId: lock.offrampId, chain: lock.chain, inrPaise: u64(lock.inrPaise), user: addr(lock.user as `0x${string}`),
    lockedAt: u64(lock.lockedAt), amount: lock.amount,
  };
  const options = {
    expiration: ExpirationTime.fromMinutes(10),
    flags: { readonly: true, permissionlessExtension: false },
    salt: BigInt(lock.offrampId.slice(0, 34)), // the first 128 bits
  };

  const estimate = await estimateWrite(account, accessKey, (unsigned) => unsigned.createEntity(Job, values, options));
  const txParams = await txParamsFor(reader, account.address, estimate, MAX_COST_WEI);
  console.log(`estimate ${estimate} gas; would send gas ${txParams.gas}, nonce ${txParams.nonce}, cost ${txParams.gas! * txParams.gasPrice!} wei at most`);
  if (!send) {
    console.log("estimate only: nothing was signed. Add --yes to write.");
    return;
  }

  journal({ op: "createJob", offrampId: lock.offrampId, owner: account.address, ...txParams, state: "intent" });
  const result = await wallet.createEntity(Job, values, options, txParams);
  journal({ op: "createJob", offrampId: lock.offrampId, state: "sent", txHash: result.txHash, entityKey: result.entityKey });

  const receipt = await reader.getTransactionReceipt({ hash: result.txHash });
  if (receipt.status !== "success") throw new Error(`write reverted, reconcile before retrying: ${result.txHash}`);
  const entity = await reader.getEntity(result.entityKey, Job);
  if (entity.offrampId.value !== lock.offrampId || entity.inrPaise.value !== lock.inrPaise || entity.$creator.toLowerCase() !== account.address.toLowerCase() || !entity.$creationFlags.readonly) {
    throw new Error(`read-back mismatch, don't resend: ${result.entityKey}`);
  }
  journal({ op: "createJob", offrampId: lock.offrampId, state: "verified", entityKey: result.entityKey });
  console.log(`entity ${result.entityKey}`);
  console.log(`tx     ${result.txHash}`);
  console.log(`owner  ${entity.$owner}`);
  console.log(`expires at block ${result.expiresAt}`);
  console.log(`read back: ${entity.chain.value}, ${entity.inrPaise.value} paise, locked at ${entity.lockedAt.value}`);
}

main().catch((error: unknown) => {
  // SDK and viem errors can include the RPC URL, which contains the access key, so only known messages are shown.
  const message = error instanceof Error ? error.message : "";
  const safe = /^(wrong chain|no locks|ARKIV_ACCESS_KEY|unsigned estimate|the estimate|gas |the wallet|this wallet|write reverted|read-back)/.test(message);
  console.error(safe ? message : `Arkiv request failed (${error instanceof Error ? error.name : "unknown"}). Check the journal before retrying.`);
  process.exit(1);
});
