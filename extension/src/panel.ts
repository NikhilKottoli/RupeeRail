/** The side panel: goal, live steps, outcome and the kill switch. */

const SERVER = "http://127.0.0.1:8790";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const form = $<HTMLFormElement>("start-form");
const goal = $<HTMLTextAreaElement>("goal");
const maxInr = $<HTMLInputElement>("max-inr");
const stop = $<HTMLButtonElement>("stop");
const resume = $<HTMLButtonElement>("resume");
const outcome = $("outcome");
const shot = $<HTMLImageElement>("shot");
const log = $<HTMLOListElement>("log");
const wallet = $("wallet");

const TONES: Record<string, string> = {
  PAID: "good", DRY_RUN: "warn", CHECK_THE_PHONE: "warn", NO_RESULT: "warn", NOT_PAID: "bad", REFUSED: "bad", FAILED: "bad", STOPPED: "warn",
};
const TITLES: Record<string, string> = {
  PAID: "Paid", DRY_RUN: "Dry run", CHECK_THE_PHONE: "Check the phone", NO_RESULT: "No result", NOT_PAID: "Not paid", REFUSED: "Refused", FAILED: "Failed", STOPPED: "Stopped",
};

function setRunning(running: boolean): void {
  form.hidden = running;
  stop.hidden = !running;
  stop.disabled = false;
  if (!running) resume.hidden = true;
}

function showOutcome(status: string, message: string): void {
  outcome.hidden = false;
  outcome.className = TONES[status] ?? "";
  outcome.textContent = `${TITLES[status] ?? status}: ${message}`;
}

// Everything here is set as text, because event messages can contain text from the web page.
function addStep(event: any): void {
  const item = document.createElement("li");
  const stage = document.createElement("span");
  stage.className = "stage";
  stage.textContent = `${event.stage}: `;
  item.append(stage, event.message ?? "");
  if (event.model) {
    const model = document.createElement("span");
    model.className = "model";
    model.textContent = ` (${event.model})`;
    item.append(model);
  }
  log.prepend(item);
}

function onEvent(event: any): void {
  addStep(event);
  if (typeof event.screenshot === "string") {
    shot.src = `data:image/jpeg;base64,${event.screenshot}`;
    shot.hidden = false;
  }
  if (event.stage === "need_help") {
    resume.hidden = false;
    showOutcome("CHECK_THE_PHONE", event.message);
    outcome.textContent = `The agent needs help: ${event.message}`;
  }
  if (event.stage === "result") showOutcome(event.status, event.message);
}

async function targetTab(): Promise<chrome.tabs.Tab | undefined> {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  const pages = tabs.filter((t) => t.url?.startsWith("http"));
  return pages.find((t) => t.active) ?? pages.sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0))[0];
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  outcome.hidden = true;
  log.replaceChildren();
  shot.hidden = true;
  const tab = await targetTab();
  if (!tab?.id) {
    showOutcome("FAILED", "open the store's page first.");
    return;
  }
  setRunning(true);
  const reply = await chrome.runtime.sendMessage({ kind: "start", goal: goal.value.trim(), maxInr: Number(maxInr.value), tabId: tab.id, windowId: tab.windowId });
  if (!reply?.ok) {
    setRunning(false);
    showOutcome("FAILED", reply?.error ?? "couldn't start the agent.");
  }
});

stop.addEventListener("click", () => {
  stop.disabled = true;
  void chrome.runtime.sendMessage({ kind: "stop" });
});

resume.addEventListener("click", () => {
  resume.hidden = true;
  outcome.hidden = true;
  void chrome.runtime.sendMessage({ kind: "resume" });
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.kind === "event") onEvent(message.event);
  else if (message.kind === "ended") setRunning(false);
  else if (message.kind === "error") showOutcome("FAILED", message.error);
});

async function loadWallet(): Promise<void> {
  try {
    const response = await fetch(`${SERVER}/wallet`);
    if (!response.ok) throw new Error();
    const w = await response.json();
    const short = `${w.address.slice(0, 6)}…${w.address.slice(-4)}`;
    wallet.textContent = `Wallet ${short} · ${w.balance} ${w.symbol} on ${w.chain}`;
  } catch {
    wallet.textContent = "Can't reach the agent server. Start it, then reopen this panel.";
  }
}

void loadWallet();
chrome.runtime.sendMessage({ kind: "status" }).then((status) => {
  setRunning(!!status?.running);
  for (const event of status?.history ?? []) onEvent(event);
}).catch(() => undefined);

export {};
