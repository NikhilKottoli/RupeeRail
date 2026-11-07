/**
 * Reads RupeeRail's public record straight from Arkiv with your own access key, without any RupeeRail server:
 *
 *   ARKIV_ACCESS_KEY=<your key> npm run verify
 *   ARKIV_ACCESS_KEY=<your key> npm run verify -- <agent wallet>
 *
 * Get a free key at https://hub.arkiv.network/access-keys. It only reads, so it needs no wallet or funds.
 */

import { makeReader } from "./arkiv.ts";
import { allReceipts, findCard, foreignCards, listCards, statsFrom } from "./reads.ts";
import { PUBLISHER } from "./schema.ts";

const key = process.env.ARKIV_ACCESS_KEY;
if (!key) {
  console.error("Set ARKIV_ACCESS_KEY. Get a free key at https://hub.arkiv.network/access-keys");
  process.exit(1);
}
const reader = makeReader(key);
const wallet = process.argv[2];

try {
  console.log(`Only entities created by ${PUBLISHER} count.`);
  console.log(`Cards from any other wallet, which are ignored: ${await foreignCards(reader)}\n`);
  const cards = wallet ? [await findCard(reader, wallet)].flatMap((card) => (card ? [card] : [])) : await listCards(reader);
  if (!cards.length) console.log("No cards found.");
  for (const card of cards) {
    const receipts = await allReceipts(reader, card.wallet);
    const stats = statsFrom(receipts);
    console.log(`${card.name} (${card.category}) ${card.wallet}`);
    console.log(`  entity ${card.key}, listed until about ${card.expiresAt}`);
    console.log(`  ${stats.paid} paid, ${stats.notPaid} not paid, ${stats.unknown} unknown. Rs ${Number(stats.paidInrPaise) / 100} paid in total`);
    for (const r of receipts) {
      console.log(`  ${new Date(r.finishedAt).toISOString()} ${r.outcome} Rs ${Number(r.inrPaise) / 100} https://explorer.testnet.arc.io/tx/${r.lockTx}`);
    }
    console.log();
  }
} catch (error) {
  // SDK errors can contain the RPC URL, which contains the access key.
  console.error(`Arkiv couldn't be read (${error instanceof Error ? error.name : "unknown"}).`);
  process.exit(1);
}
