/**
 * Reads the page and carries out actions, in every frame. Nothing here is specific to one site.
 * It holds no secrets, and only runs when the service worker asks.
 */

type PageAction = { type: string; target?: string | null; text?: string | null };

const SELECTOR = "a[href],button,input,select,textarea,summary,label,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=option],[onclick]";
const MAX_CANDIDATES = 80;
const MAX_PRIORITY = 40;
const MAX_PAGE_TEXT = 4500;
const MAX_CONTEXT = 160;
const CONTEXT_LEVELS = 8;
const MAX_CONTEXT_SCAN = 800;
const CLIP_LEVELS = 5;
const MIN_COVERED_FOR_DIALOG = 3;
const DIALOG_COVERED_SHARE = 0.5;
const AGENT_ATTR = "data-agent-id";
const MAYBE_PAYMENT = /upi|scan|qr\b/i;
// Likely steps of a purchase. These stay in the list however far they are from the middle of the screen.
const IMPORTANT = /upi|scan|qr\b|pay|proceed|checkout|check out|buy|\badd\b|\bcart\b|\border\b|continue|use this|deliver|search|sign in/i;
// The way to the next step of a purchase. A list of products is full of "Add" buttons, and these often sit far from the
// middle of the screen, such as a cart in the top bar, so they're always kept.
const CHECKOUT = /\bcart\b|\bbasket\b|\bbag\b|checkout|check out|proceed|\bupi\b|\bscan\b|\d+\s*items?\b/i;
const MAX_CHECKOUT = 12;
const MAX_STYLED_SCAN = 4000;
const MAX_STYLED_TEXT = 80;
const QUIET_MS = 1000; // a response often takes a second or two, and nothing on the page moves until it arrives
const SETTLE_CAP_MS = 3000;
const SETTLE_POLL_MS = 100;
const LOADING = '[aria-busy="true"],[class*="skeleton" i],[class*="shimmer" i],[class*="spinner" i],[class*="loader" i]';
// Lines worth keeping when a long page has to be cut short.
const KEY_LINE = /total|amount|payable|pay|upi|₹|deliver|sign in|captcha|one.time|otp|cart|basket|\bitems?\b|added/i;

function isChoice(element: Element): boolean {
  return element instanceof HTMLInputElement && (element.type === "radio" || element.type === "checkbox");
}

function isVisible(element: Element): boolean {
  // Styled radio buttons hide the real input, so judge those by what the user sees next to them.
  const shown = isChoice(element) ? (element as HTMLInputElement).labels?.[0] ?? element.parentElement ?? element : element;
  const rect = shown.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return false;
  const style = getComputedStyle(shown);
  if (style.visibility === "hidden" || style.display === "none" || (!isChoice(element) && Number(style.opacity) === 0)) return false;
  // Also catches a parent that hides it, such as a site's invisible panel of keyboard shortcuts.
  if (shown.checkVisibility && !shown.checkVisibility({ opacityProperty: !isChoice(element), visibilityProperty: true, contentVisibilityAuto: true })) return false;
  return isChoice(element) || !isClippedAway(shown);
}

/** The usual way to hide something for everyone but screen readers: a parent clipped to nothing. */
function isClippedAway(element: Element): boolean {
  for (let node: Element | null = element, level = 0; node && node !== document.body && level < CLIP_LEVELS; node = node.parentElement, level++) {
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    if (style.clipPath === "inset(50%)" || style.clip === "rect(0px, 0px, 0px, 0px)" || style.clip === "rect(1px, 1px, 1px, 1px)") return true;
    if (rect.width <= 2 && rect.height <= 2 && style.overflow !== "visible") return true;
  }
  return false;
}

/**
 * On screen, but a click on it would land on the page behind it, so a person can't see or click it. Some sites hide
 * controls for screen readers and keyboards this way, and offering them sends the agent after buttons that do nothing.
 */
function isGhost(element: Element, r: DOMRect): boolean {
  if (isChoice(element)) return false;
  const x = r.x + r.width / 2;
  const y = r.y + r.height / 2;
  if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
  const under = document.elementFromPoint(x, y);
  return !!under && under !== element && !element.contains(under) && under.contains(element);
}

/**
 * Many sites build controls such as a cart from a plain div with a click handler, which no selector can find. The pointer
 * cursor gives them away. This keeps only the outermost one, and skips anything holding a real control, such as a product card.
 */
function clickableByStyle(): HTMLElement[] {
  const found: HTMLElement[] = [];
  const blocks = document.body?.querySelectorAll<HTMLElement>("div,span,li,section,header,footer,nav,aside") ?? [];
  let scanned = 0;
  for (const element of blocks) {
    if (++scanned > MAX_STYLED_SCAN) break;
    const text = (element.textContent ?? "").trim();
    if ((!text && !element.getAttribute("aria-label")) || text.length > MAX_STYLED_TEXT) continue;
    if (element.closest(SELECTOR) || element.querySelector(SELECTOR)) continue;
    if (getComputedStyle(element).cursor !== "pointer") continue;
    const parent = element.parentElement;
    if (parent && getComputedStyle(parent).cursor === "pointer") continue;
    found.push(element);
  }
  return found;
}

function roleOf(element: Element): string {
  const role = element.getAttribute("role");
  if (role) return role;
  const tag = element.tagName.toLowerCase();
  if (tag === "a") return "link";
  if (tag === "summary" || tag === "label") return "button";
  if (tag === "input") {
    const type = (element as HTMLInputElement).type;
    return ["button", "submit", "reset"].includes(type) ? "button" : ["checkbox", "radio"].includes(type) ? type : "input";
  }
  if (tag === "textarea") return "input";
  return tag;
}

function isSensitive(element: Element): boolean {
  if (!(element instanceof HTMLInputElement)) return false;
  const name = `${element.name} ${element.id} ${element.autocomplete}`;
  return element.type === "password" || /cc-|one-time-code|password|card.?number|cvv|cvc|\bpin\b/i.test(name);
}

function labelOf(element: Element): string {
  const clean = (text: string | null | undefined) => (text ?? "").replace(/\s+/g, " ").trim().slice(0, 120);
  if (element instanceof HTMLInputElement) {
    if (isChoice(element)) return clean(element.labels?.[0]?.textContent ?? element.closest("label, li, div")?.textContent);
    if (["button", "submit"].includes(element.type)) return clean(element.value || element.closest("span,div")?.textContent);
    return clean(element.labels?.[0]?.textContent);
  }
  return clean(element.textContent);
}

/**
 * What's around an element, such as the dish beside an "Add" button or the restaurant beside "View menu". Many of a
 * page's buttons read the same, so this is often the only way to tell them apart. It's the text of the largest block
 * holding this button and no other with the same label: a dish's whole card, but not the list of dishes.
 */
function contextOf(element: Element, label: string): string {
  let context = "";
  let node = element.parentElement;
  for (let level = 0; node && node !== document.body && level < CONTEXT_LEVELS; level++, node = node.parentElement) {
    if ([...node.querySelectorAll(SELECTOR)].some((other) => other !== element && !other.contains(element) && !element.contains(other) && labelOf(other) === label)) break;
    const text = (node.innerText ?? "").replace(/\s+/g, " ").trim();
    if (text.length > MAX_CONTEXT_SCAN) break;
    if (text.length > label.length + 2) context = text;
  }
  return context.slice(0, MAX_CONTEXT);
}

/** Only where it's needed, since it adds to what's sent to the model on every step. */
function needsContext(element: Element, label: string, all: Element[]): boolean {
  if (element instanceof HTMLInputElement && !isChoice(element)) return false;
  // A bare "1" or "+" says nothing by itself: it could be a quantity in a product card, or the cart.
  return label.length <= 2 || /\badd\b/i.test(label) || all.some((other) => other !== element && labelOf(other) === label);
}

/** Brings a likely QR into view, so the screenshot contains it. */
function showLikelyQr(pageText: string): void {
  if (!MAYBE_PAYMENT.test(pageText)) return;
  const squareEnough = (e: Element) => {
    const { width, height } = e.getBoundingClientRect();
    return width >= 120 && height >= 120 && Math.max(width, height) / Math.min(width, height) < 1.6;
  };
  const media = [...document.querySelectorAll("img,canvas,svg")].filter((e) => isVisible(e) && squareEnough(e));
  const named = media.find((e) => /qr|upi/i.test(`${e.getAttribute("alt") ?? ""} ${e.getAttribute("src") ?? ""} ${e.getAttribute("aria-label") ?? ""}`));
  (named ?? media[0])?.scrollIntoView({ block: "center" });
}

function readText(): string {
  const full = (document.body?.innerText ?? "").replace(/\n{3,}/g, "\n\n");
  if (full.length <= MAX_PAGE_TEXT) return full;
  const keyLines = full.split("\n").filter((line) => line.trim().length < 140 && KEY_LINE.test(line));
  return `${keyLines.join("\n").slice(0, MAX_PAGE_TEXT / 2)}\n\n${full.slice(0, MAX_PAGE_TEXT / 2)}`;
}

/** Whether a real click on the middle of this element, as it is now on screen, would hit something else. */
function isCovered(element: Element, r: DOMRect): boolean {
  const shown = isChoice(element) ? (element as HTMLInputElement).labels?.[0] ?? element.parentElement ?? element : element;
  const box = shown === element ? r : shown.getBoundingClientRect();
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
  const under = document.elementFromPoint(x, y);
  return !!under && !shown.contains(under) && !under.contains(shown);
}

/**
 * Resolves once the page has stopped changing, so the agent doesn't pay a model to read a half-loaded one. Quiet means no
 * new content, no finished requests and no visible loading placeholder. A page that never stops, such as a carousel, gets the cap.
 */
function settled(quietMs = QUIET_MS, capMs = SETTLE_CAP_MS): Promise<void> {
  return new Promise((resolve) => {
    const started = performance.now();
    let last = started;
    let requests = performance.getEntriesByType("resource").length;
    const observer = new MutationObserver(() => { last = performance.now(); });
    observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    const timer = setInterval(() => {
      const now = performance.now();
      const seen = performance.getEntriesByType("resource").length;
      if (seen !== requests) {
        requests = seen;
        last = now;
      }
      const loading = [...document.querySelectorAll(LOADING)].some((e) => e.getBoundingClientRect().width > 0);
      if (now - started >= capMs || (now - last >= quietMs && !loading)) {
        clearInterval(timer);
        observer.disconnect();
        resolve();
      }
    }, SETTLE_POLL_MS);
  });
}

function snapshot() {
  const pageText = readText();
  showLikelyQr(pageText);
  document.querySelectorAll(`[${AGENT_ATTR}]`).forEach((e) => e.removeAttribute(AGENT_ATTR));
  const middle = window.innerHeight / 2;
  const distance = (r: DOMRect) => Math.abs(r.top + r.height / 2 - middle);
  const styled = new Set<Element>(clickableByStyle());
  let all = [...document.querySelectorAll(SELECTOR), ...styled]
    .filter((e) => isVisible(e) && !isSensitive(e) && !(e as HTMLButtonElement).disabled)
    // A label for a choice would just repeat the choice.
    .filter((e) => !(e instanceof HTMLLabelElement && e.control && isChoice(e.control)))
    .map((e) => ({ e, r: e.getBoundingClientRect(), label: `${labelOf(e)} ${e.getAttribute("aria-label") ?? ""} ${(e as HTMLInputElement).placeholder ?? ""}` }))
    .sort((a, b) => distance(a.r) - distance(b.r));
  // A dialog over the page: its controls are the only ones that can be used, and listing the rest misleads a model.
  const onScreen = all.filter((c) => c.r.bottom > 0 && c.r.top < window.innerHeight && c.r.right > 0 && c.r.left < window.innerWidth);
  all = all.filter((c) => !isGhost(c.e, c.r));
  const usable = onScreen.filter((c) => !isGhost(c.e, c.r) && !isCovered(c.e, c.r));
  const coveredCount = onScreen.length - usable.length;
  if (usable.length && coveredCount >= MIN_COVERED_FOR_DIALOG && coveredCount / onScreen.length >= DIALOG_COVERED_SHARE) all = usable;
  const dialogOpen = all === usable;
  const checkout = all.filter((c) => CHECKOUT.test(c.label) && !/\badd\b/i.test(c.label)).slice(0, MAX_CHECKOUT);
  const priority = all.filter((c) => IMPORTANT.test(c.label) || isChoice(c.e)).slice(0, MAX_PRIORITY);
  const chosen = new Set([...checkout, ...priority]);
  for (const c of all) {
    if (chosen.size >= MAX_CANDIDATES) break;
    chosen.add(c);
  }
  const elements = all.map((c) => c.e);
  const candidates = [...chosen].sort((a, b) => distance(a.r) - distance(b.r)).map(({ e, r }, index) => {
    const id = `a${index + 1}`;
    e.setAttribute(AGENT_ATTR, id);
    const field = e as HTMLInputElement;
    return {
      id,
      role: styled.has(e) ? "clickable" : roleOf(e),
      text: labelOf(e),
      context: needsContext(e, labelOf(e), elements) ? contextOf(e, labelOf(e)) : "",
      ariaLabel: e.getAttribute("aria-label") ?? "",
      placeholder: field.placeholder ?? "",
      value: !isChoice(e) && typeof field.value === "string" && e instanceof HTMLInputElement && e.type !== "submit" ? field.value.slice(0, 100) : "",
      href: e instanceof HTMLAnchorElement ? e.href : "",
      checked: isChoice(e) ? field.checked : e.getAttribute("aria-checked") === "true",
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      inViewport: r.bottom > 0 && r.top < window.innerHeight,
    };
  });
  return { url: location.href, title: document.title, page_text: pageText, candidates, dialog_open: dialogOpen };
}

function findTarget(id: string | null | undefined): HTMLElement {
  const element = id ? document.querySelector<HTMLElement>(`[${AGENT_ATTR}="${CSS.escape(id)}"]`) : null;
  if (!element) throw new Error(`${id} is no longer on the page`);
  return element;
}

/** Where a real click on this element would land, in the page's viewport. Choices are clicked directly instead. */
function locateClick(element: HTMLElement): { ok: boolean; x?: number; y?: number; covered?: boolean; error?: string; landed_on?: string } {
  element.closest("a")?.removeAttribute("target");
  element.closest("form")?.removeAttribute("target");
  element.scrollIntoView({ block: "center", behavior: "instant" });
  if (isChoice(element)) return { ok: true };
  const r = element.getBoundingClientRect();
  const x = r.x + r.width / 2;
  const y = r.y + r.height / 2;
  const under = document.elementFromPoint(x, y);
  // A real click goes to whatever is on top, so a pop-up has to be closed first.
  if (under && !element.contains(under)) return { ok: false, covered: true, x, y, error: "something is covering that element, such as a pop-up, or it's hidden. Close the pop-up, or choose another element" };
  const hit = under ?? element;
  const link = hit.closest("a")?.getAttribute("href") ?? "";
  return { ok: true, x, y, landed_on: `${labelOf(hit).slice(0, 60)}${link ? ` -> ${link.slice(0, 60)}` : ""}` };
}

function click(element: HTMLElement): void {
  // Stores often open products in a new tab, and the agent keeps working in this one.
  element.closest("a")?.removeAttribute("target");
  element.closest("form")?.removeAttribute("target");
  element.scrollIntoView({ block: "center", behavior: "instant" });
  if (isChoice(element)) {
    element.click(); // the real input may be hidden behind styling
    return;
  }
  const r = element.getBoundingClientRect();
  const x = r.x + r.width / 2;
  const y = r.y + r.height / 2;
  // A real click lands on the innermost element under the pointer, and bubbles up through every handler. Sent to an
  // outer element instead, it never reaches a handler on something inside it.
  const under = document.elementFromPoint(x, y);
  const target = under && element.contains(under) ? under : element;
  const init = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, button: 0 };
  const pointer = { ...init, pointerId: 1, pointerType: "mouse", isPrimary: true };
  target.dispatchEvent(new PointerEvent("pointermove", pointer));
  target.dispatchEvent(new MouseEvent("mousemove", init));
  target.dispatchEvent(new PointerEvent("pointerdown", { ...pointer, buttons: 1 }));
  target.dispatchEvent(new MouseEvent("mousedown", { ...init, buttons: 1 }));
  element.focus?.();
  target.dispatchEvent(new PointerEvent("pointerup", pointer));
  target.dispatchEvent(new MouseEvent("mouseup", init));
  target.dispatchEvent(new MouseEvent("click", init));
}

function typeInto(element: HTMLElement, text: string): void {
  const field = element as HTMLInputElement | HTMLTextAreaElement;
  if (isSensitive(element)) throw new Error("the agent doesn't type into sensitive fields");
  field.focus();
  // Frameworks such as React watch the native setter, so it must be called directly.
  const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(field, text);
  field.dispatchEvent(new Event("input", { bubbles: true }));
  field.dispatchEvent(new Event("change", { bubbles: true }));
}

function pressKey(key: string): void {
  const target = (document.activeElement as HTMLElement | null) ?? document.body;
  for (const type of ["keydown", "keyup"]) target.dispatchEvent(new KeyboardEvent(type, { key, bubbles: true, cancelable: true }));
  if (key === "Enter") (target as HTMLInputElement).form?.requestSubmit?.();
}

function perform(action: PageAction): { ok: boolean; error?: string; x?: number; y?: number; covered?: boolean; landed_on?: string } {
  try {
    switch (action.type) {
      case "locate_click":
        return locateClick(findTarget(action.target));
      case "click":
        click(findTarget(action.target));
        break;
      case "type":
        typeInto(findTarget(action.target), action.text ?? "");
        break;
      case "scroll":
        window.scrollBy({ top: (action.text === "up" ? -1 : 1) * window.innerHeight * 0.8 });
        break;
      case "key":
        pressKey(action.text ?? "");
        break;
      default:
        throw new Error(`unsupported action ${action.type}`);
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "the action failed" };
  }
}

declare global {
  interface Window {
    __rupeerail?: { snapshot: typeof snapshot; perform: typeof perform; settled: typeof settled };
  }
}

window.__rupeerail = { snapshot, perform, settled };

export {};
