/**
 * Connects a tab to the agent server. It stays on the tab the run started in, and refuses to read or act on any site
 * outside that agent's allowed sites, whatever the server asks.
 *
 * Many sites ignore scripted clicks, so clicks are sent as real input through chrome.debugger. It's attached only to the
 * run's tab, after the site check, and only during a run. Chrome shows its banner meanwhile. If the person cancels that
 * banner, the run carries on with scripted clicks and doesn't attach again.
 */

import { getConnection } from "./connection";

const SETTLE_MS = 400;
const PING_MS = 20_000;
const SCROLL_SETTLE_MS = 150;
const NEW_TAB_WAIT_MS = 20_000;
const STAGE_LIMIT_MS = 12_000;
const CAPTURE_LIMIT_MS = 4_000;
const CONTENT_SETTLE_LIMIT_MS = 4_000;
const HOVER_SETTLE_MS = 400;
const DEBUGGER_VERSION = "1.3";

interface AgentLook {
  name: string;
  avatar: string;
  colour: string;
}

interface Run {
  runId: string;
  agent: AgentLook;
  prompt: string;
  tabId: number;
  windowId: number;
  allowed: string[];
  socket: WebSocket;
  ping: number;
  debuggerTab: number | null;
  debuggerRefused: boolean;
}

let run: Run | null = null;
let history: unknown[] = [];

function toPanel(message: unknown): void {
  chrome.runtime.sendMessage(message).catch(() => undefined); // the panel may be closed
}

function hostAllowed(url: string | undefined, allowed: string[]): boolean {
  if (!url) return false;
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return false;
  }
  return allowed.some((d) => host === d.toLowerCase() || host.endsWith(`.${d.toLowerCase()}`));
}

function hostOf(url: string | undefined): string {
  try {
    return new URL(url ?? "").hostname.toLowerCase();
  } catch {
    return "";
  }
}

class OffSite extends Error {
  constructor(readonly host: string) {
    super("the tab is outside the allowed sites");
  }
}

async function guardedTab(current: Run): Promise<chrome.tabs.Tab> {
  const tab = await chrome.tabs.get(current.tabId);
  if (!hostAllowed(tab.url, current.allowed)) throw new OffSite(hostOf(tab.url));
  return tab;
}

/** Reads every frame on an allowed site. Ad and third-party frames stay out of it. */
async function readFrames(current: Run) {
  await chrome.scripting.executeScript({ target: { tabId: current.tabId, allFrames: true }, files: ["content.js"] });
  const results = await chrome.scripting.executeScript({
    target: { tabId: current.tabId, allFrames: true },
    func: () => window.__rupeerail?.snapshot(),
  });
  const top = results.find((r) => r.frameId === 0)?.result;
  if (!top) throw new Error("couldn't read the page");
  let pageText = top.page_text;
  const candidates = top.candidates;
  const skipped = new Set<string>();
  for (const { frameId, result } of results) {
    if (frameId === 0 || !result) continue;
    if (!hostAllowed(result.url, current.allowed)) {
      if (hostOf(result.url)) skipped.add(hostOf(result.url)); // reported by a rehearsal, as it may be a payment gateway
      continue;
    }
    pageText += `\n\n${result.page_text}`;
    for (const c of result.candidates) candidates.push({ ...c, id: `f${frameId}:${c.id}` });
  }
  return { ...top, page_text: pageText, candidates, skipped_hosts: [...skipped] };
}

async function performInFrame(current: Run, action: any) {
  const match = /^f(\d+):(.+)$/.exec(action.target ?? "");
  const frameIds = [match ? Number(match[1]) : 0];
  const [reply] = await chrome.scripting.executeScript({
    target: { tabId: current.tabId, frameIds },
    func: (a) => window.__rupeerail?.perform(a),
    args: [{ ...action, target: match ? match[2] : action.target }],
  });
  return reply?.result;
}

function settle(tabId: number): Promise<void> {
  return new Promise((resolve) => {
    const started = Date.now();
    const check = async () => {
      const tab = await chrome.tabs.get(tabId).catch(() => null);
      if (!tab || tab.status === "complete" || Date.now() - started > 8000) resolve();
      else setTimeout(check, 150);
    };
    setTimeout(check, SETTLE_MS);
  });
}

/** Names the stage that's stuck, because "the extension didn't respond" doesn't help anyone fix it. */
function within<T>(work: Promise<T>, stage: string, ms = STAGE_LIMIT_MS): Promise<T> {
  return Promise.race([work, new Promise<T>((_, reject) => setTimeout(() => reject(new Error(`the browser got stuck while ${stage}`)), ms))]);
}

/** Chrome's own capture can hang when its window isn't visible, so this falls back to the debugger's capture. */
async function screenshot(current: Run): Promise<string> {
  try {
    const dataUrl = await within(chrome.tabs.captureVisibleTab(current.windowId, { format: "jpeg", quality: 85 }), "capturing the window", CAPTURE_LIMIT_MS);
    return dataUrl.split(",")[1];
  } catch {
    if (!(await attachDebugger(current))) throw new Error("couldn't take a screenshot. Is the Chrome window minimized or the screen locked?");
    const shot = await within(
      chrome.debugger.sendCommand({ tabId: current.tabId }, "Page.captureScreenshot", { format: "jpeg", quality: 85 }) as Promise<{ data: string }>,
      "taking a screenshot. Is the Chrome window minimized or the screen locked?",
    );
    return shot.data;
  }
}

/** Waits in every frame, because a payment form often loads inside one while the page around it stays still. */
async function contentSettled(tabId: number): Promise<void> {
  const target = { tabId, allFrames: true };
  await chrome.scripting.executeScript({ target, files: ["content.js"] });
  await chrome.scripting.executeScript({ target, func: () => window.__rupeerail?.settled() });
}

async function observe(current: Run) {
  const tab = await guardedTab(current);
  await settle(current.tabId);
  await within(contentSettled(current.tabId), "waiting for the page to load", CONTENT_SETTLE_LIMIT_MS).catch(() => undefined); // it's only a courtesy
  const snapshot = await within(readFrames(current), "reading the page");
  await new Promise((resolve) => setTimeout(resolve, 150)); // let a QR that was just scrolled to paint
  if (!tab.active) await chrome.tabs.update(current.tabId, { active: true });
  return { type: "observation", ...snapshot, screenshot_b64: await screenshot(current) };
}

async function detachDebugger(current: Run): Promise<void> {
  const tabId = current.debuggerTab;
  current.debuggerTab = null;
  if (tabId !== null) await chrome.debugger.detach({ tabId }).catch(() => undefined);
}

async function attachDebugger(current: Run): Promise<boolean> {
  if (current.debuggerRefused) return false;
  if (current.debuggerTab === current.tabId) return true;
  await detachDebugger(current); // a click may have moved the run to a new tab
  try {
    await chrome.debugger.attach({ tabId: current.tabId }, DEBUGGER_VERSION);
    current.debuggerTab = current.tabId;
    // Pages throttle their animations when the window isn't in front, and dialogs can get stuck halfway.
    await chrome.debugger.sendCommand({ tabId: current.tabId }, "Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => undefined);
    return true;
  } catch {
    return false;
  }
}

async function realClick(current: Run, x: number, y: number): Promise<void> {
  const send = (type: string, extra: object = {}) =>
    chrome.debugger.sendCommand({ tabId: current.tabId }, "Input.dispatchMouseEvent", { type, x, y, ...extra });
  await send("mouseMoved");
  await send("mousePressed", { button: "left", buttons: 1, clickCount: 1 });
  await send("mouseReleased", { button: "left", buttons: 0, clickCount: 1 });
}

/** Returns a reply if it clicked or found a reason not to, or null to fall back to a scripted click. */
async function clickWithRealInput(current: Run, action: any) {
  if (/^f\d+:/.test(action.target ?? "")) return null; // coordinates inside a frame aren't the page's
  const locate = { type: "locate_click", target: action.target };
  let first = await performInFrame(current, locate); // scrolls it into view
  if (first?.covered && (await attachDebugger(current))) {
    // The last click leaves the pointer where it was, so a hover effect on the next page, such as a product photo's zoom,
    // can sit over the button. Nothing the agent chooses would close it, but moving the pointer does, as it would for a person.
    await chrome.debugger.sendCommand({ tabId: current.tabId }, "Input.dispatchMouseEvent", { type: "mouseMoved", x: first.x, y: first.y });
    await new Promise((resolve) => setTimeout(resolve, HOVER_SETTLE_MS));
    first = await performInFrame(current, locate);
  }
  if (!first?.ok) return first;
  if (first.x === undefined) return null; // a radio or checkbox, whose real input may be hidden
  await new Promise((resolve) => setTimeout(resolve, SCROLL_SETTLE_MS));
  const spot = await performInFrame(current, locate); // the layout can shift as a page scrolls
  if (!spot?.ok) return spot;
  if (!(await attachDebugger(current))) return null;
  await chrome.tabs.update(current.tabId, { active: true });
  await realClick(current, spot.x as number, spot.y as number);
  return { ok: true, landed_on: spot.landed_on };
}

async function act(current: Run, action: any) {
  await guardedTab(current);
  if (action.type === "back") {
    // Only to a page this tab has already been on. If that's off the agent's sites, the next read stops the run.
    await chrome.tabs.goBack(current.tabId).catch(() => {
      throw new Error("there's no earlier page to go back to");
    });
    await settle(current.tabId);
    return { type: "action_result", ok: true, error: null };
  }
  const clicked = action.type === "click" ? await clickWithRealInput(current, action) : null;
  const reply = clicked ?? await performInFrame(current, action);
  await settle(current.tabId);
  return { type: "action_result", ok: !!reply?.ok, error: reply?.error ?? null, landed_on: reply?.landed_on ?? null };
}

function finish(current: Run): void {
  clearInterval(current.ping);
  void detachDebugger(current);
  if (run === current) run = null;
  toPanel({ kind: "ended" });
}

async function handle(current: Run, message: any): Promise<void> {
  const send = (reply: object) => current.socket.send(JSON.stringify(reply));
  if (message.type === "observe") {
    send(await observe(current).catch((e) => ({ type: "error", error: e instanceof Error ? e.message : "couldn't read the page", host: e instanceof OffSite ? e.host : undefined })));
  } else if (message.type === "action") {
    send(await act(current, message.action).catch((e) => ({ type: "action_result", ok: false, error: e instanceof Error ? e.message : "the action failed" })));
  } else if (message.type === "event") {
    const { screenshot, ...event } = message;
    history = [...history.slice(-200), event];
    toPanel({ kind: "event", event: message });
  } else if (message.type === "finished") {
    current.socket.close();
  }
}

/** The tab the person is on, if it's already on one of the agent's sites. Otherwise the agent's own start page. */
async function chooseTab(allowed: string[], startUrl: string): Promise<chrome.tabs.Tab> {
  const tabs = await chrome.tabs.query({ lastFocusedWindow: true });
  const pages = tabs.filter((t) => t.url?.startsWith("http"));
  const here = pages.find((t) => t.active) ?? pages.sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0))[0];
  if (here && hostAllowed(here.url, allowed)) return here;
  const opened = await chrome.tabs.create({ url: startUrl, active: true, windowId: tabs[0]?.windowId });
  return addressKnown(opened.id!);
}

/** A new tab has no address until its page starts arriving, and the site check needs one. */
async function addressKnown(tabId: number): Promise<chrome.tabs.Tab> {
  const started = Date.now();
  for (;;) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.url?.startsWith("http") || Date.now() - started > NEW_TAB_WAIT_MS) return tab;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
}

async function start(agentId: string, agent: AgentLook, prompt: string, maxInr: number, rehearse: boolean) {
  if (run) throw new Error("an agent is already running");
  const connection = await getConnection();
  const response = await fetch(`${connection.server}/agents/${agentId}/runs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ prompt, max_inr: maxInr, rehearse }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "the agent server refused");
  const tab = await chooseTab(body.allowed_domains, body.start_url);
  await chrome.windows.update(tab.windowId, { focused: true }).catch(() => undefined); // once, so the page is visible as it runs
  history = [];
  const socket = new WebSocket(`${connection.socket}/runs/${body.run_id}`);
  const current: Run = {
    runId: body.run_id, agent, prompt, tabId: tab.id!, windowId: tab.windowId, allowed: body.allowed_domains, socket,
    debuggerTab: null, debuggerRefused: false,
    ping: setInterval(() => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify({ type: "ping" })), PING_MS) as unknown as number,
  };
  run = current;
  socket.onmessage = (e) => void handle(current, JSON.parse(e.data));
  socket.onclose = () => finish(current);
  socket.onerror = () => toPanel({ kind: "error", error: "lost contact with the agent server" });
  return { runId: body.run_id, maxInr: body.max_inr, allowed: body.allowed_domains };
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.tab?.url?.startsWith("http")) return; // never from a page's content script
  if (message.kind === "start") {
    start(message.agentId, message.agent, message.prompt, message.maxInr, !!message.rehearse)
      .then((result) => reply({ ok: true, ...result }))
      .catch((e) => reply({ ok: false, error: e instanceof Error ? e.message : "couldn't start" }));
    return true;
  }
  if (message.kind === "stop") {
    if (run?.socket.readyState === WebSocket.OPEN) run.socket.send(JSON.stringify({ type: "stop" }));
    reply({ ok: true });
  } else if (message.kind === "resume") {
    if (run?.socket.readyState === WebSocket.OPEN) run.socket.send(JSON.stringify({ type: "resume" }));
    reply({ ok: true });
  } else if (message.kind === "status") {
    reply({ running: !!run, history, agent: run?.agent, prompt: run?.prompt });
  }
});

// A click can still open a new tab. Follow it, and the site check applies there too.
chrome.tabs.onCreated.addListener((tab) => {
  if (run && tab.id !== undefined && tab.openerTabId === run.tabId) run.tabId = tab.id;
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (!run || source.tabId !== run.debuggerTab) return;
  run.debuggerTab = null;
  if (reason === "canceled_by_user") run.debuggerRefused = true;
});

/**
 * Development only: when the server runs with AGENT_DEV_LOOP=1, a local tool can queue rehearsals and have the extension
 * reload itself after a rebuild. Otherwise the server answers 404, and this does nothing. Everything here is a rehearsal,
 * which can't order or pay, and the server's Origin check keeps websites out.
 */
async function pollDevQueue(): Promise<void> {
  if (run) return;
  const connection = await getConnection();
  if (connection.hosted) return; // a hosted server has no development queue
  const response = await fetch(`${connection.server}/dev/next`).catch(() => null);
  if (!response?.ok) return;
  const next = await response.json();
  if (next.reload) chrome.runtime.reload();
  else if (next.agent_id) await start(next.agent_id, next.agent, next.prompt, next.max_inr, true).catch((e) => console.warn("queued rehearsal failed", e));
}

chrome.alarms.create("dev-queue", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "dev-queue") void pollDevQueue();
});

chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true }).catch(() => undefined);

export {};
