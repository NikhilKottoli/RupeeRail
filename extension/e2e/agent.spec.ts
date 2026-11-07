/**
 * The real extension in Chromium, the real agent server and the real mock stores, against a fake off-ramp.
 * Each agent gets a throwaway local key in place of a Circle wallet. No testnet, wallet or phone is involved.
 */

import { test as base, chromium, expect, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const STORE = "http://127.0.0.1:5181/";
const FAKE = "http://127.0.0.1:8791";
const AGENT = "http://127.0.0.1:8790";

const CHEF = {
  name: "Chef", description: "Orders food", avatar: "chef", colour: "saffron", instructions: "Order what the person asks for.", profile: "",
  sites: ["127.0.0.1"], start_url: STORE, per_order_max_inr: 20, budget_usdc: "2",
};

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
  test.skip(!existsSync(path.join(import.meta.dirname, "../../apps/mock-store/foodie/index.html")), "needs the mock stores, which are kept locally and not in git");
  await request.post(`${FAKE}/__reset`, { data: { payer: "paid" } });
  for (const agent of await (await request.get(`${AGENT}/agents`)).json()) await request.delete(`${AGENT}/agents/${agent.id}`);
});

async function makeAgent(request: APIRequestContext, panel: Page, over: Partial<typeof CHEF> = {}) {
  const response = await request.post(`${AGENT}/agents`, { data: { ...CHEF, ...over } });
  expect(response.status(), await response.text()).toBe(201);
  const { id } = await response.json();
  await expect.poll(async () => (await (await request.get(`${AGENT}/agents/${id}`)).json()).wallet_status).toBe("ready");
  await panel.reload();
  return (await request.get(`${AGENT}/agents/${id}`)).json();
}

async function runAgent(panel: Page, name: string, prompt: string, limit = "1", rehearse = false) {
  await panel.locator(".agent-card", { hasText: name }).click();
  await panel.getByLabel(`What should ${name} do?`).fill(prompt);
  await panel.getByLabel("Limit for this order (₹)").fill(limit);
  if (rehearse) await panel.getByRole("checkbox", { name: /Rehearse/ }).check();
  await panel.getByRole("button", { name: "Run", exact: true }).click();
}

const SIDE_PANEL = { width: 400, height: 860 };

const outcome = (panel: Page) => panel.locator("#outcome");
const quotes = async (request: APIRequestContext) => (await (await request.get(`${FAKE}/__state`)).json()).quotes;

test("choosing an agent and typing a prompt ends in a real result, from the page's own QR", async ({ panel, store, request }) => {
  await makeAgent(request, panel);
  const card = panel.locator(".agent-card", { hasText: "Chef" });
  await expect(card.locator(".amount")).toHaveText("2.00");
  await expect(card.locator(".unit")).toHaveText("USDC left");
  await expect(card.locator(".avatar.colour-saffron img")).toHaveAttribute("src", "avatars/chef.svg");
  await runAgent(panel, "Chef", "Order a cutting chai");

  await expect(panel.locator("#run-name")).toHaveText("Chef");
  await expect(panel.locator("#run-avatar img")).toHaveAttribute("src", "avatars/chef.svg");
  await expect(panel.locator("#run-state")).toContainText("Working");
  await expect(outcome(panel)).toContainText("Paid: The merchant was paid", { timeout: 60_000 });
  await expect(panel.locator("#log li .avatar img").first()).toHaveAttribute("src", "avatars/chef.svg");
  await expect(panel.locator("#run-state")).toBeHidden();

  // The agent really drove the store, in the tab the person was on.
  await expect(store.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
  await expect(store.locator("#pay-total")).toHaveText("₹1");

  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes).toHaveLength(1);
  expect(state.quotes[0].inr).toBe("1.00");
  expect(state.quotes[0].qrText).toMatch(/^upi:\/\/pay\?pa=[^&]+@/); // read from a JPEG of the visible tab
  expect(state.submits).toHaveLength(1);
  expect(state.submits[0].userSig).toMatch(/^0x[0-9a-f]{130}$/);

  await expect(panel.locator("#log")).toContainText(process.env.E2E_OPENAI ? "openai:" : "rule-based");
  if (process.env.E2E_SCREENSHOTS) await panel.screenshot({ path: "e2e/screenshots/run.png" });
  await expect(panel.locator("#shot")).toBeVisible();

  const webhooks = await (await request.get(`${AGENT}/webhooks/test`)).json();
  expect(webhooks.at(-1)).toMatchObject({ status: "PAID", amount_inr: 1, prompt: "Order a cutting chai", agent_name: "Chef" });

  // It stays in the agent's history, and the budget has fallen.
  await panel.getByRole("button", { name: "Done" }).click();
  await expect(panel.locator("#history")).toContainText("Order a cutting chai");
  await expect(panel.locator("#history")).toContainText("Paid");
  await expect(panel.locator("#history")).toContainText("₹1");
  await expect(panel.locator("#agent-wallet .amount")).toHaveText("1.98"); // cut to two decimals, and never rounded up
  await expect(panel.locator("#agent-wallet .budget-figure")).toHaveAttribute("title", "1.989894 USDC");
  if (process.env.E2E_SCREENSHOTS) {
    await panel.setViewportSize(SIDE_PANEL);
    await panel.waitForTimeout(400);
    await panel.screenshot({ path: "e2e/screenshots/agent.png", fullPage: true });
  }
});

test("on-chain COMPLETED isn't reported as paid when the phone failed", async ({ panel, request }) => {
  await makeAgent(request, panel);
  await request.post(`${FAKE}/__reset`, { data: { payer: "failed" } });
  await runAgent(panel, "Chef", "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Not paid", { timeout: 60_000 });
  await expect(outcome(panel)).toContainText("dispute");
});

test("a price over the limit is refused, and nothing is quoted", async ({ panel, store, request }) => {
  await makeAgent(request, panel);
  await runAgent(panel, "Chef", "Order a masala chai", "1"); // it costs ₹20
  await expect(outcome(panel)).toContainText("Refused", { timeout: 60_000 });
  await expect(outcome(panel)).toContainText("over the 100 paise limit");
  await expect(store.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
  expect(await quotes(request)).toHaveLength(0);
});

test("a prompt can't raise the agent's limit", async ({ panel, request }) => {
  await makeAgent(request, panel, { per_order_max_inr: 1 });
  await panel.locator(".agent-card", { hasText: "Chef" }).click();
  await expect(panel.getByLabel("Limit for this order (₹)")).toHaveValue("1");
  await panel.getByLabel("What should Chef do?").fill("Order a masala chai");
  // The form's own limit can be bypassed by a person, so the server is what's being checked here.
  await panel.getByLabel("Limit for this order (₹)").evaluate((input: HTMLInputElement) => { input.removeAttribute("max"); input.value = "500"; });
  await panel.getByRole("button", { name: "Run", exact: true }).click();
  await expect(outcome(panel)).toContainText("over the 100 paise limit", { timeout: 60_000 });
  expect(await quotes(request)).toHaveLength(0);
});

test("an item that doesn't exist asks for help, and Stop ends it", async ({ panel, request }) => {
  await makeAgent(request, panel);
  await runAgent(panel, "Chef", "Order a pizza");
  await expect(outcome(panel)).toContainText("Chef needs help", { timeout: 30_000 });
  await expect(panel.locator("#run-state")).toContainText("Waiting for you");
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(outcome(panel)).toContainText("Stopped", { timeout: 15_000 });
  await expect(panel.getByRole("button", { name: "Done" })).toBeVisible();
  expect(await quotes(request)).toHaveLength(0);
});

test("from a tab on another site, the agent opens its own start page and leaves that tab alone", async ({ panel, store, context, request }) => {
  await makeAgent(request, panel);
  await store.goto("http://localhost:5181/"); // same page, but localhost isn't one of this agent's sites
  await store.bringToFront();
  await runAgent(panel, "Chef", "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Paid", { timeout: 60_000 });
  await expect(store.locator("#cart-count")).toHaveText("0");
  const paid = context.pages().find((p) => p.url().startsWith(STORE))!;
  await expect(paid.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
});

test("a start page that's slow to load isn't mistaken for leaving the agent's sites", async ({ panel, store, context, request }) => {
  await makeAgent(request, panel, { start_url: `${STORE}slow/` });
  await store.goto("http://localhost:5181/"); // not one of this agent's sites, so it opens the start page
  await store.bringToFront();
  await runAgent(panel, "Chef", "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Paid", { timeout: 60_000 });
  await expect(context.pages().find((p) => p.url() === STORE)!.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
});

test("the agent can go back, and the log shows what each click hit", async ({ panel, store, request }) => {
  const menu = `${STORE}foodie/?page=menu&place=annapurna`;
  await makeAgent(request, panel, { start_url: menu });
  await store.goto(menu);
  await runAgent(panel, "Chef", 'SCRIPT:["click:Foodie","back","need_help"]');
  await expect(outcome(panel)).toContainText("Chef needs help", { timeout: 30_000 });
  await expect(store).toHaveURL(menu); // it went to the home page, and came back
  await expect(panel.locator("#log")).toContainText('text "Foodie"');
  await expect(panel.locator("#log")).toContainText("click landed on: Foodie");
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(outcome(panel)).toContainText("Stopped", { timeout: 15_000 });
});

test("a queued rehearsal runs on its own, and can't pay", async ({ panel, store, request }) => {
  const agent = await makeAgent(request, panel);
  await store.bringToFront();
  expect((await request.post(`${AGENT}/dev/rehearsals`, { data: { agent_id: agent.id, prompt: "Order a cutting chai", max_inr: 1 } })).status()).toBe(202);
  await expect.poll(async () => (await (await request.get(`${AGENT}/agents/${agent.id}/runs`)).json())[0]?.status, { timeout: 70_000, intervals: [1000] }).toBe("REHEARSED");
  const [run] = await (await request.get(`${AGENT}/agents/${agent.id}/runs`)).json();
  expect(run.rehearse).toBe(1);
  expect(run.detail).toContain("Stopped before paying, at the UPI QR");
  expect(await quotes(request)).toHaveLength(0);
});

test("a website can't queue a rehearsal", async ({ store }) => {
  const status = await store.evaluate((agent) => fetch(`${agent}/dev/rehearsals`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ agent_id: "x", prompt: "y" }),
  }).then((r) => r.status).catch(() => "blocked"), AGENT);
  expect([403, "blocked"]).toContain(status);
});

test("a tab that moves off the agent's sites is never read or paid", async ({ panel, store, request }) => {
  await makeAgent(request, panel);
  await runAgent(panel, "Chef", "Order a pizza");
  await expect(outcome(panel)).toContainText("Chef needs help", { timeout: 30_000 });
  await store.goto("http://localhost:5181/");
  await panel.getByRole("button", { name: "I've handled it, continue" }).click();
  await expect(outcome(panel)).toContainText("Failed", { timeout: 30_000 });
  await expect(outcome(panel)).toContainText("outside the allowed sites");
  expect(await quotes(request)).toHaveLength(0);
});

test("a website can't reach the agent server", async ({ store }) => {
  const result = await store.evaluate(async (agent) => {
    const http = await fetch(`${agent}/agents`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: "evil" }),
    }).then((r) => r.status).catch(() => "blocked");
    const socket = await new Promise<string>((resolve) => {
      const ws = new WebSocket(`${agent.replace("http", "ws")}/runs/anything`);
      ws.onopen = () => resolve("open");
      ws.onerror = () => resolve("refused");
      ws.onclose = () => resolve("refused");
    });
    return { http, socket };
  }, AGENT);
  expect([403, "blocked"]).toContain(result.http);
  expect(result.socket).toBe("refused");
});

test("creating an agent from a template, choosing its avatar, editing and archiving it", async ({ panel, request }) => {
  await expect(panel.getByText("No agents yet")).toBeVisible();
  await panel.getByRole("button", { name: "New agent" }).click();
  await expect(panel.getByLabel("Name")).toHaveValue("Chef");
  await expect(panel.getByLabel("Instructions")).toHaveValue(/diet and allergies come first/);
  await expect(panel.locator("#avatar-preview img")).toHaveAttribute("src", "avatars/chef.svg");
  await expect(panel.locator("#avatars [role=radio]")).toHaveCount(12);
  if (process.env.E2E_SCREENSHOTS) await panel.screenshot({ path: "e2e/screenshots/create.png", fullPage: true });

  // A start page off the agent's sites is refused, with the reason shown.
  await panel.getByLabel("Name").fill("Sous Chef");
  await panel.getByLabel("Things it should know").fill("Diet: vegetarian");
  await panel.getByLabel("Allowed sites, one per line").fill("127.0.0.1");
  await panel.getByLabel("Start page").fill("https://evil.example/");
  await panel.getByRole("button", { name: "Create agent" }).click();
  await expect(panel.locator("#edit-error")).toContainText("start URL must be on one of the agent's sites");

  await panel.getByLabel("Start page").fill(STORE);
  await panel.getByLabel("Limit per order (₹)").fill("25");
  await panel.getByRole("radio", { name: "scientist" }).click();
  await panel.getByRole("radio", { name: "teal" }).click();
  await expect(panel.locator("#avatar-preview.colour-teal img")).toHaveAttribute("src", "avatars/scientist.svg");
  await panel.getByRole("button", { name: "Create agent" }).click();

  // The new agent appears with its wallet and on-chain budget.
  await expect(panel.locator("#agent-name")).toHaveText("Sous Chef");
  await expect(panel.locator("#agent-avatar.colour-teal img")).toHaveAttribute("src", "avatars/scientist.svg");
  await expect(panel.locator("#agent-wallet")).toContainText("5.00 USDC budget left", { timeout: 15_000 });
  await expect(panel.getByLabel("Limit for this order (₹)")).toHaveValue("25");
  await expect(panel.getByRole("button", { name: "Run", exact: true })).toBeEnabled();

  // It lasts: a fresh panel still has it, and what it knows comes back in the editor.
  await panel.reload();
  await panel.locator(".agent-card", { hasText: "Sous Chef" }).click();
  await panel.getByRole("button", { name: "Edit agent" }).click();
  await expect(panel.getByLabel("Things it should know")).toHaveValue("Diet: vegetarian");

  await panel.getByLabel("Name").fill("Head Chef");
  await panel.getByRole("radio", { name: "artist" }).click();
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(panel.locator("#agent-name")).toHaveText("Head Chef");
  await expect(panel.locator("#agent-avatar img")).toHaveAttribute("src", "avatars/artist.svg");

  await panel.getByRole("button", { name: "Edit agent" }).click();
  await panel.getByLabel("New budget (USDC)").fill("3");
  await panel.getByRole("button", { name: "Set budget" }).click();
  await expect(panel.locator("#agent-wallet")).toContainText("3.00 USDC budget left", { timeout: 15_000 });

  panel.on("dialog", (dialog) => void dialog.accept());
  await panel.getByRole("button", { name: "Edit agent" }).click();
  await panel.getByRole("button", { name: "Archive agent" }).click();
  await expect(panel.getByText("No agents yet")).toBeVisible();
  expect(await (await request.get(`${AGENT}/agents`)).json()).toEqual([]);
});

test("two agents pay from two wallets, and only the one that paid loses budget", async ({ panel, request }) => {
  const chef = await makeAgent(request, panel);
  const barista = await makeAgent(request, panel, { name: "Barista", avatar: "artist", colour: "teal" });
  expect(barista.wallet.address).not.toBe(chef.wallet.address);
  await runAgent(panel, "Barista", "Order a cutting chai");
  await expect(outcome(panel)).toContainText("Paid", { timeout: 60_000 });
  const [quote] = await quotes(request);
  expect(quote.user.toLowerCase()).toBe(barista.wallet.address.toLowerCase());
  await panel.reload();
  await expect(panel.locator(".agent-card", { hasText: "Chef" }).locator(".amount")).toHaveText("2.00");
  await expect(panel.locator(".agent-card", { hasText: "Barista" }).locator(".amount")).toHaveText("1.98");
  await panel.setViewportSize(SIDE_PANEL); // only after the run: it resizes the whole window, including the store's tab

  // A Material 3 Expressive list: the group's outer corners are round, and the corners between items aren't.
  const radii = (card: string) => panel.locator(".agent-card", { hasText: card }).evaluate((e) => {
    const style = getComputedStyle(e);
    return [style.borderTopLeftRadius, style.borderBottomLeftRadius];
  });
  expect(await radii("Chef")).toEqual(["28px", "6px"]);
  expect(await radii("Barista")).toEqual(["6px", "28px"]);

  // The main action is short and centered, rather than a full-width bar.
  const button = (await panel.getByRole("button", { name: "New agent" }).boundingBox())!;
  expect(button.width).toBeLessThan(260);
  expect(Math.abs(button.x + button.width / 2 - 200)).toBeLessThan(2);
  if (process.env.E2E_SCREENSHOTS) {
    await panel.waitForTimeout(400);
    await panel.screenshot({ path: "e2e/screenshots/home.png" });
    await panel.emulateMedia({ colorScheme: "dark" });
    await panel.waitForTimeout(250);
    await panel.screenshot({ path: "e2e/screenshots/home-dark.png" });
    await panel.emulateMedia({ colorScheme: "light" });
  }

  // On the details page, Run and Edit agent sit side by side.
  await panel.locator(".agent-card", { hasText: "Barista" }).click();
  const run = (await panel.getByRole("button", { name: "Run", exact: true }).boundingBox())!;
  const editAgent = (await panel.getByRole("button", { name: "Edit agent" }).boundingBox())!;
  expect(Math.abs(run.y - editAgent.y)).toBeLessThan(2);
  expect(editAgent.x).toBeGreaterThan(run.x + run.width - 1);
});

test("a rehearsal reaches the QR, then stops without quoting or paying", async ({ panel, store, request }) => {
  await makeAgent(request, panel);
  await runAgent(panel, "Chef", "Order a cutting chai", "1", true);
  await expect(outcome(panel)).toContainText("Rehearsed: Stopped before paying, at the UPI QR", { timeout: 60_000 });
  await expect(outcome(panel)).toContainText("UPI QR readable: yes");
  await expect(store.getByRole("heading", { name: "Pay with UPI" })).toBeVisible();
  const state = await (await request.get(`${FAKE}/__state`)).json();
  expect(state.quotes).toHaveLength(0);
  expect(state.submits).toHaveLength(0);
  await panel.getByRole("button", { name: "Done" }).click();
  await expect(panel.locator("#history")).toContainText("Rehearsed");
});

test("a marketplace flow: new tab, hidden radios, COD preselected, QR in a frame", async ({ panel, store, context, request }) => {
  test.skip(!process.env.E2E_OPENAI, "needs the real model: set E2E_OPENAI=1");
  test.setTimeout(280_000);
  await makeAgent(request, panel, { name: "Shopper", avatar: "shopper", instructions: "Buy what the person asks for.", start_url: `${STORE}bazaar/` });
  await store.goto(`${STORE}bazaar/`);
  await runAgent(panel, "Shopper", "Buy a blue gel pen");
  await expect(outcome(panel)).toContainText("Paid: The merchant was paid", { timeout: 260_000 });
  const pages = context.pages().map((p) => p.url());
  console.log("steps:\n" + (await panel.locator("#log li").allInnerTexts()).reverse().join("\n"));
  console.log("tabs:", pages.join(" "));
  const shop = context.pages().find((p) => p.url().includes("bazaar") && !p.url().includes("chrome-extension"))!;
  expect(await shop.evaluate(() => localStorage.getItem("spent_without_upi"))).toBeNull();
  expect(await shop.evaluate(() => localStorage.getItem("method"))).toBe('"upi"');
  const [quote] = await quotes(request);
  expect(quote.inr).toBe("1.00");
  expect(quote.qrText).toMatch(/^upi:\/\/pay\?pa=[^&]+@/);
});

const CHEF_INSTRUCTIONS = "You order food for this person from food delivery sites. Choose one dish that fits their request and everything you know about them. Their diet comes first. Don't ask them to choose: pick one yourself.";

test("publishing is off by default, and the switch saves", async ({ panel, request }) => {
  await makeAgent(request, panel, { name: "Quiet" } as Partial<typeof CHEF>);
  await panel.getByRole("button", { name: /Quiet/ }).click();
  await panel.getByRole("button", { name: "Edit agent" }).click();
  await expect(panel.getByLabel("Publish a public track record")).not.toBeChecked();
  await expect(panel.getByLabel("Category")).toHaveValue(/.+/);
  await panel.getByLabel("Publish a public track record").check();
  await panel.getByLabel("Category").selectOption("groceries");
  await panel.getByRole("button", { name: "Save" }).click();
  await panel.getByRole("button", { name: "Edit agent" }).click();
  await expect(panel.getByLabel("Publish a public track record")).toBeChecked();
  await expect(panel.getByLabel("Category")).toHaveValue("groceries");
});

async function orderDinner(panel: Page, store: Page, request: APIRequestContext, profile: string, prompt = "Order dinner", limit = "1") {
  await makeAgent(request, panel, { instructions: CHEF_INSTRUCTIONS, profile, start_url: `${STORE}foodie/` });
  await store.goto(`${STORE}foodie/`);
  await runAgent(panel, "Chef", prompt, limit);
  try {
    await expect(outcome(panel)).toBeVisible({ timeout: 240_000 });
  } finally {
    console.log("steps:\n" + (await panel.locator("#log li").allInnerTexts()).reverse().join("\n"));
  }
}

test("a real-style menu: each ADD is told apart", async ({ store }) => {
  await store.goto(`${STORE}foodie/?page=menu&place=annapurna`);
  await store.evaluate(readFileSync(path.join(import.meta.dirname, "../dist/content.js"), "utf8"));
  const snapshot = await store.evaluate(() => (window as any).__rupeerail.snapshot());
  const adds = snapshot.candidates.filter((c: any) => c.text === "ADD");
  expect(adds).toHaveLength(3);
  // Invisible keyboard controls aren't offered, because clicking them does nothing.
  expect(snapshot.candidates.filter((c: any) => /shift|keyboard/.test(c.text))).toEqual([]);
  for (const dish of ["Dal khichdi", "Paneer butter masala", "Royal thali"]) {
    expect(adds.filter((c: any) => c.context.includes(dish))).toHaveLength(1);
  }
});

test("while a dialog covers the page, only its controls are listed", async ({ store }) => {
  await store.goto(`${STORE}foodie/`);
  const code = readFileSync(path.join(import.meta.dirname, "../dist/content.js"), "utf8");
  await store.evaluate(code);
  const withPopup = await store.evaluate(() => (window as any).__rupeerail.snapshot());
  expect(withPopup.dialog_open).toBe(true);
  expect(withPopup.candidates.map((c: any) => c.text).sort()).toEqual(["Join Gold", "No thanks"]);
  await store.getByRole("button", { name: "Close offer" }).click();
  const without = await store.evaluate(() => (window as any).__rupeerail.snapshot());
  expect(without.dialog_open).toBe(false);
  expect(without.candidates.map((c: any) => c.text)).toContain("View menu");
});

test("a site that ignores scripted clicks still gets a real click on ADD, inside the button", async ({ panel, store, request }) => {
  const menu = `${STORE}foodie/?page=menu&place=annapurna`;
  await makeAgent(request, panel, { start_url: menu });
  await store.goto(menu);
  await runAgent(panel, "Chef", "Order a dal khichdi");
  await expect(store.locator("#cart-link")).toHaveText("Cart (1)", { timeout: 30_000 });
  // Later, the rule-based decider won't choose UPI, so the order click is blocked and it asks for help.
  await expect(outcome(panel)).toContainText("Chef needs help", { timeout: 30_000 });
  expect(await store.evaluate(() => localStorage.getItem("ordered"))).toBeNull();
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(outcome(panel)).toContainText("Stopped", { timeout: 15_000 });
});

test("a hover zoom left open by the pointer doesn't stop a real click on the button it covers", async ({ panel, store, request }) => {
  await makeAgent(request, panel, { start_url: `${STORE}hover/` });
  await store.goto(`${STORE}hover/`);
  await store.bringToFront();
  await runAgent(panel, "Chef", 'SCRIPT:["click:Open product","click:Add to cart","need_help"]');
  await expect(store.locator("#add")).toHaveText("Added", { timeout: 30_000 });
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(outcome(panel)).toContainText("Stopped", { timeout: 15_000 });
});

test("the same prompt gets a vegetarian dish for a vegetarian", async ({ panel, store, request }) => {
  test.skip(!process.env.E2E_OPENAI, "needs the real model: set E2E_OPENAI=1");
  test.setTimeout(280_000);
  await orderDinner(panel, store, request, "Diet: strictly vegetarian");
  await expect(outcome(panel)).toContainText("Paid: The merchant was paid", { timeout: 240_000 });
  if (process.env.E2E_SCREENSHOTS) await panel.screenshot({ path: "e2e/screenshots/chef.png" });
  const ordered: string[] = JSON.parse((await store.evaluate(() => localStorage.getItem("ordered")))!);
  console.log("ordered:", ordered);
  expect(ordered.length).toBeGreaterThan(0);
  expect(ordered.join(" ")).toMatch(/khichdi|paneer|pulao/i);
  expect(ordered.join(" ")).not.toMatch(/chicken/i);
  expect(await store.evaluate(() => localStorage.getItem("spent_without_upi"))).toBeNull();
  expect(await store.evaluate(() => localStorage.getItem("joined_gold"))).toBeNull();
});

test("the same prompt gets chicken for someone who wants chicken", async ({ panel, store, request }) => {
  test.skip(!process.env.E2E_OPENAI, "needs the real model: set E2E_OPENAI=1");
  test.setTimeout(280_000);
  await orderDinner(panel, store, request, "Diet: not vegetarian. Loves chicken, and wants it at every meal");
  await expect(outcome(panel)).toContainText("Paid: The merchant was paid", { timeout: 240_000 });
  const ordered: string[] = JSON.parse((await store.evaluate(() => localStorage.getItem("ordered")))!);
  console.log("ordered:", ordered);
  expect(ordered.join(" ")).toMatch(/chicken/i);
  expect(await store.evaluate(() => localStorage.getItem("spent_without_upi"))).toBeNull();
});

test("a dish over the limit is not ordered", async ({ panel, store, request }) => {
  test.skip(!process.env.E2E_OPENAI, "needs the real model: set E2E_OPENAI=1");
  test.setTimeout(280_000);
  await orderDinner(panel, store, request, "Diet: strictly vegetarian", "Order the Royal thali");
  console.log("outcome:", await outcome(panel).innerText());
  await expect(outcome(panel)).not.toContainText("Paid");
  expect(await store.evaluate(() => localStorage.getItem("ordered"))).toBeNull();
  expect(await quotes(request)).toHaveLength(0);
  await panel.getByRole("button", { name: "Stop" }).click().catch(() => undefined);
});

test("real Amazon pages, logged out, in a fresh profile", async ({ panel, store, context, request }) => {
  test.skip(!process.env.E2E_AMAZON, "set E2E_AMAZON=1. Nothing can be bought without an account");
  test.setTimeout(280_000);
  await makeAgent(request, panel, { name: "Shopper", instructions: "Buy what the person asks for.", sites: ["amazon.in"], start_url: "https://www.amazon.in/", per_order_max_inr: 80 });
  await store.goto("https://www.amazon.in/");
  await runAgent(panel, "Shopper", "Buy a ball pen", "80");
  await expect(outcome(panel)).toBeVisible({ timeout: 240_000 });
  console.log("outcome:", await outcome(panel).innerText());
  console.log("steps:\n" + (await panel.locator("#log li").allInnerTexts()).reverse().join("\n"));
  console.log("tabs:", context.pages().map((p) => p.url().slice(0, 90)).join(" | "));
  await panel.getByRole("button", { name: "Stop" }).click().catch(() => undefined);
});

test("with no server of its own, the extension can use the hosted one, and a run works over it", async ({ panel, request }) => {
  const HOSTED = "http://127.0.0.1:8794";
  const made = await request.post(`${HOSTED}/agents`, { data: CHEF });
  expect(made.status(), await made.text()).toBe(201);
  const { id } = await made.json();
  await expect.poll(async () => (await (await request.get(`${HOSTED}/agents/${id}`)).json()).wallet_status).toBe("ready");

  await panel.route(`${AGENT}/**`, (route) => route.abort()); // nothing is listening on this computer
  await panel.evaluate((server) => chrome.storage.local.set({ hostedServer: server }), HOSTED);
  await panel.reload();
  await expect(panel.locator("#home-error")).toContainText("Can't reach an agent server");
  await expect(panel.getByRole("button", { name: "New agent" })).toBeHidden();

  await panel.getByRole("button", { name: "Use RupeeRail's server" }).click();
  await expect(panel.locator("#hosted-note")).toContainText("keep personal details out");
  await runAgent(panel, "Chef", "Order a cutting chai", "1", true);
  await expect(outcome(panel)).toContainText("Rehearsed", { timeout: 60_000 });

  await panel.getByRole("button", { name: "Done" }).click();
  await panel.locator("[data-to='home']").first().click();
  await panel.getByRole("button", { name: "Stop using it" }).click();
  await expect(panel.locator("#home-error")).toContainText("Can't reach an agent server");
  await expect(panel.getByRole("button", { name: "Use RupeeRail's server" })).toBeVisible();
  await request.delete(`${HOSTED}/agents/${id}`);
});
