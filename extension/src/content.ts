/**
 * Reads the page and carries out actions, in every frame. Nothing here is specific to one site.
 * It holds no secrets, and only runs when the service worker asks.
 */

type PageAction = { type: string; target?: string | null; text?: string | null };

const SELECTOR = "a[href],button,input,select,textarea,summary,label,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=option],[onclick]";
const MAX_CANDIDATES = 80;
const MAX_PRIORITY = 40;
const MAX_PAGE_TEXT = 4500;
const AGENT_ATTR = "data-agent-id";
const MAYBE_PAYMENT = /upi|scan|qr\b/i;
// Likely steps of a purchase. These stay in the list however far they are from the middle of the screen.
const IMPORTANT = /upi|scan|qr\b|pay|proceed|checkout|check out|buy|add to (cart|basket|bag)|\bcart\b|place (your )?order|continue|use this|deliver|search|sign in/i;
// Lines worth keeping when a long page has to be cut short.
const KEY_LINE = /total|amount|payable|pay|upi|₹|deliver|sign in|captcha|one.time|otp/i;

function isChoice(element: Element): boolean {
  return element instanceof HTMLInputElement && (element.type === "radio" || element.type === "checkbox");
}

function isVisible(element: Element): boolean {
  // Styled radio buttons hide the real input, so judge those by what the user sees next to them.
  const shown = isChoice(element) ? (element as HTMLInputElement).labels?.[0] ?? element.parentElement ?? element : element;
  const rect = shown.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return false;
  const style = getComputedStyle(shown);
  return style.visibility !== "hidden" && style.display !== "none" && (isChoice(element) || Number(style.opacity) !== 0);
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

function snapshot() {
  const pageText = readText();
  showLikelyQr(pageText);
  document.querySelectorAll(`[${AGENT_ATTR}]`).forEach((e) => e.removeAttribute(AGENT_ATTR));
  const middle = window.innerHeight / 2;
  const distance = (r: DOMRect) => Math.abs(r.top + r.height / 2 - middle);
  const all = [...document.querySelectorAll(SELECTOR)]
    .filter((e) => isVisible(e) && !isSensitive(e) && !(e as HTMLButtonElement).disabled)
    // A label for a choice would just repeat the choice.
    .filter((e) => !(e instanceof HTMLLabelElement && e.control && isChoice(e.control)))
    .map((e) => ({ e, r: e.getBoundingClientRect(), label: `${labelOf(e)} ${e.getAttribute("aria-label") ?? ""} ${(e as HTMLInputElement).placeholder ?? ""}` }))
    .sort((a, b) => distance(a.r) - distance(b.r));
  const priority = all.filter((c) => IMPORTANT.test(c.label) || isChoice(c.e)).slice(0, MAX_PRIORITY);
  const chosen = new Set(priority);
  for (const c of all) {
    if (chosen.size >= MAX_CANDIDATES) break;
    chosen.add(c);
  }
  const candidates = [...chosen].sort((a, b) => distance(a.r) - distance(b.r)).map(({ e, r }, index) => {
    const id = `a${index + 1}`;
    e.setAttribute(AGENT_ATTR, id);
    const field = e as HTMLInputElement;
    return {
      id,
      role: roleOf(e),
      text: labelOf(e),
      ariaLabel: e.getAttribute("aria-label") ?? "",
      placeholder: field.placeholder ?? "",
      value: !isChoice(e) && typeof field.value === "string" && e instanceof HTMLInputElement && e.type !== "submit" ? field.value.slice(0, 100) : "",
      href: e instanceof HTMLAnchorElement ? e.href : "",
      checked: isChoice(e) ? field.checked : e.getAttribute("aria-checked") === "true",
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      inViewport: r.bottom > 0 && r.top < window.innerHeight,
    };
  });
  return { url: location.href, title: document.title, page_text: pageText, candidates };
}

function findTarget(id: string | null | undefined): HTMLElement {
  const element = id ? document.querySelector<HTMLElement>(`[${AGENT_ATTR}="${CSS.escape(id)}"]`) : null;
  if (!element) throw new Error(`${id} is no longer on the page`);
  return element;
}

function click(element: HTMLElement): void {
  // Stores often open products in a new tab, and the agent keeps working in this one.
  element.closest("a")?.removeAttribute("target");
  element.closest("form")?.removeAttribute("target");
  element.scrollIntoView({ block: "center" });
  if (isChoice(element)) {
    element.click(); // the real input may be hidden behind styling
    return;
  }
  const r = element.getBoundingClientRect();
  const init = { bubbles: true, cancelable: true, view: window, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, button: 0 };
  element.dispatchEvent(new PointerEvent("pointerdown", init));
  element.dispatchEvent(new MouseEvent("mousedown", init));
  element.focus?.();
  element.dispatchEvent(new PointerEvent("pointerup", init));
  element.dispatchEvent(new MouseEvent("mouseup", init));
  element.dispatchEvent(new MouseEvent("click", init));
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

function perform(action: PageAction): { ok: boolean; error?: string } {
  try {
    switch (action.type) {
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
    __rupeerail?: { snapshot: typeof snapshot; perform: typeof perform };
  }
}

window.__rupeerail = { snapshot, perform };

export {};
