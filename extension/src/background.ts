/**
 * Connects the tab to the agent server. It stays on the tab the session started in, and refuses to read or act on
 * any site outside the session's allowed list, whatever the server asks.
 */

const SERVER = "http://127.0.0.1:8790";
const SOCKET = "ws://127.0.0.1:8790";
const SETTLE_MS = 400;
const PING_MS = 20_000;

interface Run {
  sessionId: string;
  tabId: number;
  windowId: number;
  allowed: string[];
  socket: WebSocket;
  ping: number;
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

async function guardedTab(current: Run): Promise<chrome.tabs.Tab> {
  const tab = await chrome.tabs.get(current.tabId);
  if (!hostAllowed(tab.url, current.allowed)) throw new Error("the tab is outside the allowed sites");
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
  for (const { frameId, result } of results) {
    if (frameId === 0 || !result || !hostAllowed(result.url, current.allowed)) continue;
    pageText += `\n\n${result.page_text}`;
    for (const c of result.candidates) candidates.push({ ...c, id: `f${frameId}:${c.id}` });
  }
  return { ...top, page_text: pageText, candidates };
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

async function observe(current: Run) {
  const tab = await guardedTab(current);
  await settle(current.tabId);
  const snapshot = await readFrames(current);
  await new Promise((resolve) => setTimeout(resolve, 150)); // let a QR that was just scrolled to paint
  if (!tab.active) await chrome.tabs.update(current.tabId, { active: true });
  const dataUrl = await chrome.tabs.captureVisibleTab(current.windowId, { format: "jpeg", quality: 85 });
  return { type: "observation", ...snapshot, screenshot_b64: dataUrl.split(",")[1] };
}

async function act(current: Run, action: object) {
  await guardedTab(current);
  const reply = await performInFrame(current, action);
  await settle(current.tabId);
  return { type: "action_result", ok: !!reply?.ok, error: reply?.error ?? null };
}

function finish(current: Run): void {
  clearInterval(current.ping);
  if (run === current) run = null;
  toPanel({ kind: "ended" });
}

async function handle(current: Run, message: any): Promise<void> {
  const send = (reply: object) => current.socket.send(JSON.stringify(reply));
  if (message.type === "observe") {
    send(await observe(current).catch((e) => ({ type: "error", error: e instanceof Error ? e.message : "couldn't read the page" })));
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

async function start(goal: string, maxInr: number, tabId: number, windowId: number) {
  if (run) throw new Error("the agent is already running");
  const response = await fetch(`${SERVER}/sessions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ goal, max_inr: maxInr }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail ?? "the agent server refused");
  history = [];
  const socket = new WebSocket(`${SOCKET}/sessions/${body.session_id}`);
  const current: Run = {
    sessionId: body.session_id, tabId, windowId, allowed: body.allowed_domains, socket,
    ping: setInterval(() => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify({ type: "ping" })), PING_MS) as unknown as number,
  };
  run = current;
  socket.onmessage = (e) => void handle(current, JSON.parse(e.data));
  socket.onclose = () => finish(current);
  socket.onerror = () => toPanel({ kind: "error", error: "lost contact with the agent server" });
  return { sessionId: body.session_id, maxInr: body.max_inr, allowed: body.allowed_domains };
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.tab?.url?.startsWith("http")) return; // never from a page's content script
  if (message.kind === "start") {
    start(message.goal, message.maxInr, message.tabId, message.windowId)
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
    reply({ running: !!run, history });
  }
});

// A click can still open a new tab. Follow it, and the site check applies there too.
chrome.tabs.onCreated.addListener((tab) => {
  if (run && tab.id !== undefined && tab.openerTabId === run.tabId) run.tabId = tab.id;
});

chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true }).catch(() => undefined);

export {};
