/**
 * The real extension in Chromium, the real agent server and the real mock store, against a fake off-ramp.
 * No testnet, wallet or phone is involved.
 */

import { test as base, chromium, expect, type BrowserContext, type Page } from "@playwright/test";
import path from "node:path";

const STORE = "http://127.0.0.1:5181/";
const FAKE = "http://127.0.0.1:8791";
const AGENT = "http://127.0.0.1:8790";

const test = base.extend<{ context: BrowserContext; panel: Page; store: Page }>({
  context: async ({}, use) => {
    const extension = path.join(import.meta.dirname, "../dist");
    const context = await chromium.launchPersistentContext("", {
      channel: "chromium",
      headless: true,
      viewport: { width: 1000, height: 720 },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    await use(context);
    await context.close();
  },
  store: async ({ context }, use) => {
    const page = await context.newPage();
    await page.goto(STORE);
    await use(page);
  },
  panel: async ({ context, store }, use) => {
    let [worker] = context.serviceWorkers();
    worker ??= await context.waitForEvent("serviceworker");
    const id = new URL(worker.url()).host;
    void store;
    const page = await context.newPage();
    await page.goto(`chrome-extension://${id}/panel.html`);
    await use(page);
  },
});

test.beforeEach(async ({ request }) => {
  await request.post(`${FAKE}/__reset`, { data: { payer: "paid" } });
});

async function startAgent(panel: Page, goal: string, limit = "1") {
  await panel.getByLabel("What should the agent buy?").fill(goal);
  await panel.getByLabel("Spending limit (₹)").fill(limit);
  await panel.getByRole("button", { name: "Start" }).click();
}

const outcome = (panel: Page) => panel.locator("#outcome");

test("a typed goal ends in a real result, from the page's own QR", async ({ panel, store, request }) => {
  await expect(panel.locator("#wallet")).toContainText("20.000000 USDC");
  await startAgent(panel, "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Paid: The merchant was paid", { timeout: 60_000 });

  // The agent really drove the store, in the user's tab.
  await expect(store.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
  await expect(store.locator("#pay-total")).toHaveText("₹1");

  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes).toHaveLength(1);
  expect(state.quotes[0].inr).toBe("1.00");
  expect(state.quotes[0].qrText).toContain("pa=vatsalgandhi239@okicici"); // read from a JPEG of the visible tab
  expect(state.submits).toHaveLength(1);
  expect(state.submits[0].userSig).toMatch(/^0x[0-9a-f]{130}$/);

  await expect(panel.locator("#log")).toContainText(process.env.E2E_OPENAI ? "openai:" : "rule-based");
  if (process.env.E2E_SCREENSHOTS) await panel.screenshot({ path: "e2e/screenshots/panel.png" });
  await expect(panel.locator("#shot")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Start" })).toBeVisible(); // ready for another goal

  const webhooks = await (await request.get(`${AGENT}/webhooks/test`)).json();
  expect(webhooks.at(-1)).toMatchObject({ status: "PAID", amount_inr: 1, goal: "Order a cutting chai" });
});

test("on-chain COMPLETED isn't reported as paid when the phone failed", async ({ panel, request }) => {
  await request.post(`${FAKE}/__reset`, { data: { payer: "failed" } });
  await startAgent(panel, "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Not paid", { timeout: 60_000 });
  await expect(outcome(panel)).toContainText("dispute");
});

test("a price over the limit is refused, and nothing is quoted", async ({ panel, store, request }) => {
  await startAgent(panel, "Order a masala chai", "1"); // it costs ₹20
  await expect(outcome(panel)).toContainText("Refused", { timeout: 60_000 });
  await expect(outcome(panel)).toContainText("over the 100 paise limit");
  await expect(store.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes).toHaveLength(0);
});

test("an item that doesn't exist asks for help, and Stop ends it", async ({ panel, request }) => {
  await startAgent(panel, "Order a pizza");
  await expect(outcome(panel)).toContainText("The agent needs help", { timeout: 30_000 });
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(outcome(panel)).toContainText("Stopped", { timeout: 15_000 });
  await expect(panel.getByRole("button", { name: "Start" })).toBeVisible();
  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes).toHaveLength(0);
});

test("a site outside the allowed list is never read or paid", async ({ panel, store, request }) => {
  await store.goto("http://localhost:5181/"); // same page, but localhost isn't allowed
  await startAgent(panel, "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Failed", { timeout: 30_000 });
  await expect(outcome(panel)).toContainText("outside the allowed sites");
  await expect(store.locator("#cart-count")).toHaveText("0");
  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes).toHaveLength(0);
});

test("a website can't reach the agent server", async ({ store }) => {
  const result = await store.evaluate(async (agent) => {
    const http = await fetch(`${agent}/sessions`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ goal: "evil", max_inr: 1 }),
    }).then((r) => r.status).catch(() => "blocked");
    const socket = await new Promise<string>((resolve) => {
      const ws = new WebSocket(`${agent.replace("http", "ws")}/sessions/anything`);
      ws.onopen = () => resolve("open");
      ws.onerror = () => resolve("refused");
      ws.onclose = () => resolve("refused");
    });
    return { http, socket };
  }, AGENT);
  expect([403, "blocked"]).toContain(result.http);
  expect(result.socket).toBe("refused");
});

test("a marketplace flow: new tab, hidden radios, COD preselected, QR in a frame", async ({ panel, store, context, request }) => {
  test.skip(!process.env.E2E_OPENAI, "needs the real model: set E2E_OPENAI=1");
  test.setTimeout(280_000);
  await store.goto(`${STORE}bazaar/`);
  await startAgent(panel, "Buy a blue gel pen");
  await expect(outcome(panel)).toContainText("Paid: The merchant was paid", { timeout: 260_000 });
  if (process.env.E2E_SCREENSHOTS) await panel.screenshot({ path: "e2e/screenshots/bazaar.png", fullPage: true });
  const pages = context.pages().map((p) => p.url());
  console.log("steps:\n" + (await panel.locator("#log li").allInnerTexts()).reverse().join("\n"));
  console.log("tabs:", pages.join(" "));
  const shop = context.pages().find((p) => p.url().includes("bazaar") && !p.url().includes("chrome-extension"))!;
  expect(await shop.evaluate(() => localStorage.getItem("spent_without_upi"))).toBeNull();
  expect(await shop.evaluate(() => localStorage.getItem("method"))).toBe('"upi"');
  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes[0].inr).toBe("1.00");
  expect(state.quotes[0].qrText).toContain("vatsalgandhi239@okicici");
});

test("real Amazon pages, logged out, in a fresh profile", async ({ panel, store, context }) => {
  test.skip(!process.env.E2E_AMAZON, "set E2E_AMAZON=1 and E2E_DOMAINS=amazon.in. Nothing can be bought without an account");
  test.setTimeout(280_000);
  await store.goto("https://www.amazon.in/");
  await startAgent(panel, "Buy a ball pen", "80");
  await expect(outcome(panel)).toBeVisible({ timeout: 240_000 });
  console.log("outcome:", await outcome(panel).innerText());
  console.log("steps:\n" + (await panel.locator("#log li").allInnerTexts()).reverse().join("\n"));
  console.log("tabs:", context.pages().map((p) => p.url().slice(0, 90)).join(" | "));
  await panel.getByRole("button", { name: "Stop" }).click().catch(() => undefined);
});
