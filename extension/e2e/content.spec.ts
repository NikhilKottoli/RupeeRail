/** What the agent can see on a page. Run with `npx playwright test -c playwright.content.config.ts`. */

import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

type Candidate = { id: string; role: string; text: string; context: string };

async function candidates(page: Page, html: string): Promise<Candidate[]> {
  await page.setContent(html);
  await page.addScriptTag({ path: path.join(import.meta.dirname, "../dist/content.js") });
  return page.evaluate(() => (window as unknown as { __rupeerail: { snapshot(): { candidates: Candidate[] } } }).__rupeerail.snapshot().candidates);
}

const product = (n: number) => `<div class="card"><p>Product ${n} ₹${n}</p><button>ADD</button></div>`;
const LIST = `<style>.card{height:90px} .cart{cursor:pointer;position:fixed;top:0;right:0}</style>`;

test("a cart built from a plain div is listed, even with a page full of Add buttons", async ({ page }) => {
  const list = await candidates(page, `${LIST}<div class="cart" id="c"><span>2 items</span> <span>₹100</span></div>${Array.from({ length: 70 }, (_, i) => product(i)).join("")}`);
  const cart = list.find((c) => c.text.includes("2 items"));
  expect(cart?.role).toBe("clickable");
  expect(list.filter((c) => c.text.includes("2 items"))).toHaveLength(1); // the outer div, not its spans as well
});

test("a quantity showing only a number says which item it belongs to", async ({ page }) => {
  const list = await candidates(page, `<div><p>Diet Coke 330 ml ₹50</p><button>-</button><button>1</button><button>+</button></div>`);
  expect(list.find((c) => c.text === "1")?.context).toContain("Diet Coke");
});

test("plain text and product cards holding a real button aren't listed as extra controls", async ({ page }) => {
  const list = await candidates(page, `<style>.card{cursor:pointer}</style><p>Just some text</p><div>A plain block</div><div class="card"><p>Tea</p><button>ADD</button></div>`);
  expect(list.map((c) => c.text)).toEqual(["ADD"]);
});

test("an Add to cart button doesn't use up the places kept for the way to checkout", async ({ page }) => {
  const buttons = Array.from({ length: 70 }, (_, i) => `<div class="card"><p>P${i}</p><button>Add to cart</button></div>`).join("");
  const list = await candidates(page, `${LIST}${buttons}<a href="/checkout">Proceed to checkout</a>`);
  expect(list.some((c) => c.text === "Proceed to checkout")).toBe(true);
});

async function settleTime(page: Page, html: string, setup: string): Promise<number> {
  await page.setContent(html);
  await page.addScriptTag({ path: path.join(import.meta.dirname, "../dist/content.js") });
  return page.evaluate(async (code) => {
    new Function(code)();
    const started = performance.now();
    await (window as unknown as { __rupeerail: { settled(): Promise<void> } }).__rupeerail.settled();
    return performance.now() - started;
  }, setup);
}

test("a page that's already still is read quickly", async ({ page }) => {
  expect(await settleTime(page, "<p>Done</p>", "")).toBeLessThan(1500);
});

test("reading waits for content that's still arriving", async ({ page }) => {
  const took = await settleTime(page, "<div id=x></div>", "setTimeout(() => { document.getElementById('x').textContent = 'loaded'; }, 900);");
  expect(took).toBeGreaterThan(1800); // the content came at 0.9s, then a quiet second
  expect(took).toBeLessThan(2800);
});

test("reading waits for a loading placeholder to go", async ({ page }) => {
  const took = await settleTime(page, '<style>.x{width:100px;height:40px}</style><div class="x skeleton-row" id=x></div>', "setTimeout(() => document.getElementById('x').className = 'x', 1200);");
  expect(took).toBeGreaterThan(1150);
  expect(took).toBeLessThan(2400);
});

test("a page that never stops changing doesn't hold the agent up for long", async ({ page }) => {
  const took = await settleTime(page, "<div id=x></div>", "setInterval(() => { document.getElementById('x').textContent = String(Math.random()); }, 100);");
  expect(took).toBeGreaterThan(2900);
  expect(took).toBeLessThan(3700);
});
