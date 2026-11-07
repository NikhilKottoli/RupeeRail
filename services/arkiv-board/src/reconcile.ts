/**
 * Shows writes with an uncertain outcome, and lets a person close one after checking it on the explorer:
 *
 *   npm run reconcile
 *   npm run reconcile -- --abandon <request id>
 *
 * Stop the publisher first. Abandoning sends nothing. It only records that this request will not be retried.
 */

import { join } from "node:path";
import { SERVICE_DIR } from "./arkiv.ts";
import { Journal } from "./publish.ts";

const journal = new Journal(join(SERVICE_DIR, "data", "publisher-journal.jsonl"));
const index = process.argv.indexOf("--abandon");
if (index > 0) {
  const id = process.argv[index + 1];
  if (!journal.unresolved().some((entry) => entry.opId === id)) throw new Error("no unresolved request has that id");
  journal.add({ opId: id, state: "abandoned" });
  console.log(`abandoned ${id}. Its effect isn't on Arkiv unless you saw it on the explorer.`);
} else {
  const open = journal.unresolved();
  if (!open.length) console.log("nothing is uncertain.");
  for (const entry of open) {
    const tx = entry.txHash ? `https://rabdi.explorer.arkiv.network/tx/${entry.txHash}` : "no transaction hash is known";
    console.log(`${entry.opId}: last step ${entry.state}, ${tx}`);
  }
}
