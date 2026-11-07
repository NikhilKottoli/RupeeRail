/** The public track records. Everything here is mocked at /arkiv, so these tests never touch Arkiv. */

import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { injectWallet } from "./support";

const WALLET = "0x179dfbC07B23541849a2Cb6D38d991D9Ec7Cda88";
const PUBLISHER = "0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c";
const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key
const HASH = `0x${"b".repeat(64)}`;

const card = (over = {}) => ({
  key: `0x${"c".repeat(64)}`, wallet: WALLET, category: "food", name: "Butler", avatar: "chef", colour: "saffron", creator: PUBLISHER,
  expiresAtBlock: "1", expiresAt: "2026-12-07T01:41:13.250Z", working: false,
  stats: { paid: 1, notPaid: 1, unknown: 0, paidInrPaise: "23200", lastPaidAt: 1794000000000 }, ...over,
});
const receipt = (over = {}) => ({
  key: `0x${"d".repeat(64)}`, wallet: WALLET, offrampId: HASH, outcome: "paid", inrPaise: "23200", finishedAt: 1794000000000, chain: "arc", lockTx: HASH, creator: PUBLISHER, expiresAt: "2026-12-07T01:41:13.250Z", ...over,
});
const meta = {
  publisher: PUBLISHER, chainId: 7794095, explorer: "https://rabdi.explorer.arkiv.network", categories: ["food", "groceries", "other"], foreignCards: 2, live: true,
  lifetimes: { cardDays: 30, receiptDays: 30, liveMinutes: 15 }, queries: { agents: `select().from(AgentCard).createdBy("${PUBLISHER}")` },
};

/** A feed the test controls, because a mocked route can't keep a stream open. */
async function fakeFeed(page: Page) {
  await page.addInitScript(() => {
    class FakeSource {
      static open: FakeSource[] = [];
      listeners: Record<string, (() => void)[]> = {};
      onerror: (() => void) | null = null;
      closed = false;
      constructor() { FakeSource.open.push(this); setTimeout(() => this.emit("ready"), 0); }
      addEventListener(name: string, fn: () => void) { (this.listeners[name] ??= []).push(fn); }
      emit(name: string) { for (const fn of this.listeners[name] ?? []) fn(); }
      close() { this.closed = true; }
    }
    (window as unknown as { EventSource: unknown }).EventSource = FakeSource;
    (window as unknown as { emitFeed: (name: string) => void }).emitFeed = (name) => { for (const s of FakeSource.open) if (!s.closed) s.emit(name); };
  });
}

async function mock(page: Page, state: { agents?: unknown[]; receipts?: unknown[]; asked?: string[] } = {}) {
  await fakeFeed(page);
  await page.route("**/arkiv/meta", (route) => route.fulfill({ json: meta }));
  await page.route(/\/arkiv\/agents(\?.*)?$/, (route) => {
    const category = new URL(route.request().url()).searchParams.get("category");
    const agents = (state.agents ?? [card()]) as { category: string }[];
    return route.fulfill({ json: { agents: category ? agents.filter((a) => a.category === category) : agents } });
  });
  await page.route(/\/arkiv\/agents\/0x[^/]+$/, (route) => route.fulfill({ json: (state.agents ?? [card()])[0] }));
  await page.route(/\/arkiv\/agents\/0x[^/]+\/receipts/, (route) => {
    state.asked?.push(new URL(route.request().url()).search);
    return route.fulfill({ json: { items: state.receipts ?? [receipt()], next: null } });
  });
}

test("anyone can open the directory without a wallet", async ({ page }) => {
  await mock(page);
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  const row = page.getByRole("list", { name: "Public agents" }).getByRole("button");
  await expect(row).toContainText("Butler");
  await expect(row).toContainText("1 paid order");
  await expect(row).toContainText("₹232");
  await expect(page.getByRole("status", { name: "Updating live" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Connect/ })).toHaveCount(0);
});

test("the connect screen links to the directory, and back returns to it", async ({ page }) => {
  await injectWallet(page, { key: KEY });
  await page.route("**/config", (route) => route.fulfill({ json: { chains: {}, limits: { minInrPaise: "100", maxInrPaise: "100000" } } }));
  await mock(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Explore the Agents Hub" }).click();
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByRole("heading", { name: "Agents Hub" })).toBeVisible();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(page.getByRole("button", { name: /^Connect/ })).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});

test("the hub hands over an extension that Chrome can load, even once the service worker is in charge", async ({ page }) => {
  await mock(page);
  await page.goto("/agents");
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);

  const box = page.getByTestId("get-the-extension");
  const [download] = await Promise.all([page.waitForEvent("download"), box.getByRole("link", { name: "Download the extension" }).click()]);
  expect(download.suggestedFilename()).toBe("rupeerail-agents-extension.zip");
  const files = execFileSync("unzip", ["-Z1", await download.path()], { encoding: "utf8" }).trim().split("\n");
  expect(files).toEqual(expect.arrayContaining(["manifest.json", "background.js", "content.js", "panel.html", "panel.js", "icons/icon-128.png"]));

  await expect(box.getByText("Choose Load unpacked")).toBeVisible();
  await expect(box.getByText("chrome://extensions")).toBeVisible();
  await expect(box.getByText("tick Rehearse")).toBeVisible();
  await expect(box.getByText("keep personal details out")).toBeVisible();
});

test("the install steps can be read before downloading", async ({ page }) => {
  await mock(page);
  await page.goto("/agents");
  const box = page.getByTestId("get-the-extension");
  await expect(box.getByText("Choose Load unpacked")).toHaveCount(0);
  await box.getByRole("button", { name: "How to install it" }).click();
  await expect(box.getByText("Choose Load unpacked")).toBeVisible();
  await expect(box.getByText("Use RupeeRail's server")).toBeVisible();
  await box.getByRole("button", { name: "Hide the steps" }).click();
  await expect(box.getByText("Choose Load unpacked")).toHaveCount(0);
});

test("Scan to pay offers the Agents Hub, in the same shape as its other actions", async ({ page }) => {
  await injectWallet(page, { key: KEY });
  await page.route("**/config", (route) => route.fulfill({ json: { chains: {}, limits: { minInrPaise: "100", maxInrPaise: "100000" } } }));
  await mock(page);
  await page.goto("/");
  await page.getByRole("button", { name: /^Connect/ }).click();
  const butler = page.getByRole("button", { name: "Make your AI butler do your work and payments" });
  const scan = (await page.getByRole("button", { name: "Scan QR code" }).boundingBox())!;
  const upload = (await page.getByRole("button", { name: "Upload a photo" }).boundingBox())!;
  const box = (await butler.boundingBox())!;
  expect(box.y).toBeGreaterThan(scan.y + scan.height - 1);
  expect(Math.abs(box.x - scan.x)).toBeLessThan(2);
  expect(Math.abs(box.x + box.width - (upload.x + upload.width))).toBeLessThan(2);
  await butler.click();
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByRole("heading", { name: "Agents Hub" })).toBeVisible();
  await expect(page.getByTestId("agents-illustration")).toBeVisible();
});

test("the extension's steps stay hidden until asked for", async ({ page }) => {
  await mock(page);
  await page.goto("/agents");
  const box = page.getByTestId("get-the-extension");
  await expect(box.getByRole("link", { name: "Download the extension" })).toHaveAttribute("href", "/downloads/rupeerail-agents-extension.zip");
  await expect(box.getByText("Load unpacked")).toHaveCount(0);
  await box.getByRole("button", { name: "How to install it" }).click();
  await expect(box.getByText("Load unpacked")).toBeVisible();
  await expect(box.getByText("A run pays for real")).toBeVisible();
});

test("an agent's page shows its record, its lease and links anyone can check", async ({ page }) => {
  await mock(page);
  await page.goto("/agents");
  await page.getByRole("button", { name: /Butler/ }).click();
  await expect(page).toHaveURL(new RegExp(`/agents/${WALLET}$`));
  await expect(page.getByTestId("paid-total")).toContainText("₹232");
  await expect(page.getByText("Each paid order renews this listing for 30 days")).toBeVisible();
  await expect(page.getByRole("link", { name: "Lock on Arc" })).toHaveAttribute("href", `https://explorer.testnet.arc.io/tx/${HASH}`);
  await expect(page.getByRole("link", { name: "Arkiv record" })).toHaveAttribute("href", `https://rabdi.explorer.arkiv.network/entity/0x${"d".repeat(64)}`);
  await expect(page.getByText("The chain can't confirm that a UPI payment arrived")).toBeVisible();

  await page.goBack();
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
});

test("a shared address opens the agent directly", async ({ page }) => {
  await mock(page);
  await page.goto(`/agents/${WALLET}`);
  await expect(page.getByRole("heading", { name: "Butler" })).toBeVisible();
});

test("the filters become a query on Arkiv's typed fields", async ({ page }) => {
  const asked: string[] = [];
  await mock(page, { asked });
  await page.goto(`/agents/${WALLET}`);
  await expect(page.getByTestId("receipt")).toHaveCount(1);
  await page.getByRole("group", { name: "Outcome" }).getByRole("button", { name: "Paid", exact: true }).click();
  await page.getByRole("group", { name: "Amount" }).getByRole("button", { name: "₹100+" }).click();
  await page.getByRole("group", { name: "Time" }).getByRole("button", { name: "7 days" }).click();
  await expect.poll(() => asked.at(-1)).toBe("?outcome=paid&minInr=100&days=7");
});

test("a new receipt appears without a refresh, and the working badge clears", async ({ page }) => {
  const state = { agents: [card({ working: true })], receipts: [receipt()] };
  await mock(page, state);
  await page.goto(`/agents/${WALLET}`);
  await expect(page.getByText("Working now")).toBeVisible();
  await expect(page.getByTestId("receipt")).toHaveCount(1);

  state.agents = [card({ working: false, stats: { paid: 2, notPaid: 1, unknown: 0, paidInrPaise: "28200", lastPaidAt: 1794000001000 } })];
  state.receipts = [receipt({ key: `0x${"e".repeat(64)}`, inrPaise: "5000", finishedAt: 1794000001000 }), receipt()];
  await page.evaluate(() => (window as unknown as { emitFeed: (name: string) => void }).emitFeed("changed"));

  await expect(page.getByTestId("receipt")).toHaveCount(2);
  await expect(page.getByTestId("paid-total")).toContainText("₹282");
  await expect(page.getByText("Working now")).toHaveCount(0);
});

test("a failure is shown as a failure, not as an empty directory", async ({ page }) => {
  await fakeFeed(page);
  await page.route("**/arkiv/meta", (route) => route.fulfill({ status: 502, json: { error: "Arkiv couldn't be read just now" } }));
  await page.route(/\/arkiv\/agents(\?.*)?$/, (route) => route.fulfill({ status: 502, json: { error: "Arkiv couldn't be read just now" } }));
  await page.goto("/agents");
  await expect(page.getByRole("alert")).toContainText("Arkiv couldn't be read just now");
  await expect(page.getByText("No agents have published")).toHaveCount(0);
});

test("a wallet with no record says so", async ({ page }) => {
  await mock(page);
  await page.route(/\/arkiv\/agents\/0x[^/]+$/, (route) => route.fulfill({ status: 404, json: { error: "no public record for this wallet" } }));
  await page.route(/\/arkiv\/agents\/0x[^/]+\/receipts/, (route) => route.fulfill({ json: { items: [], next: null } }));
  await page.goto(`/agents/${WALLET}`);
  await expect(page.getByText("This wallet has no public record")).toBeVisible();
});

test("an entity can't choose which image loads or where a link goes", async ({ page }) => {
  await mock(page, { agents: [card({ avatar: "../../evil", colour: "url(evil)" })], receipts: [receipt({ lockTx: "javascript:alert(1)", key: "javascript:alert(1)" })] });
  await page.goto(`/agents/${WALLET}`);
  await expect(page.getByTestId("receipt")).toHaveCount(1);
  await expect(page.locator('img[src="/avatars/robot.svg"]')).toHaveCount(1);
  await expect(page.getByRole("link", { name: "Lock on Arc" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Arkiv record" })).toHaveCount(0);
});

test("the queries are shown, and hidden entities are counted", async ({ page }) => {
  await mock(page);
  await page.goto("/agents");
  const box = page.getByTestId("check-it-yourself");
  await expect(box).toContainText("2 cards from other wallets are ignored");
  await box.getByRole("button", { name: "Show the queries" }).click();
  await expect(box).toContainText(`createdBy("${PUBLISHER}")`);
  await expect(box).toContainText("npm run verify");
});
