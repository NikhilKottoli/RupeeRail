/**
 * The side panel: your agents, a prompt for one of them, its live run, and creating or editing agents.
 * Everything from the server or a page is set as text, because it can contain text from the web.
 */

const SERVER = "http://127.0.0.1:8790";
const POLL_MS = 2500;

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const VIEWS = ["home", "agent", "edit", "run"] as const;
type View = (typeof VIEWS)[number];

const TONES: Record<string, string> = {
  PAID: "good", REHEARSED: "good", DRY_RUN: "warn", CHECK_THE_PHONE: "warn", NO_RESULT: "warn", INTERRUPTED: "warn", STOPPED: "warn", NOT_PAID: "bad", REFUSED: "bad", FAILED: "bad",
};
const TITLES: Record<string, string> = {
  PAID: "Paid", REHEARSED: "Rehearsed", DRY_RUN: "Dry run", CHECK_THE_PHONE: "Check the phone", NO_RESULT: "No result", INTERRUPTED: "Interrupted", NOT_PAID: "Not paid", REFUSED: "Refused", FAILED: "Failed", STOPPED: "Stopped",
};
const SETTING_UP = new Set(["pending", "creating", "funding", "approving"]);

interface Agent {
  id: string; name: string; description: string; avatar: string; colour: string; sites: string[]; start_url: string;
  per_order_max_inr: number; budget_usdc: string; wallet_status: string; wallet_detail: string;
  instructions?: string; profile?: string;
  wallet: { address: string; explorer: string; balance?: string; budget_left?: string; symbol?: string; unavailable?: boolean } | null;
}
interface Options { templates: any[]; avatars: string[]; colours: string[]; global_max_inr: number; max_budget_usdc: string; }

let options: Options | null = null;
let current: Agent | null = null;
let editing: Agent | null = null;
let pollTimer: number | undefined;
let picked = { avatar: "robot", colour: "indigo" };

function show(view: View): void {
  for (const name of VIEWS) $(`${name}-view`).hidden = name !== view;
  clearTimeout(pollTimer);
  window.scrollTo(0, 0);
}

function setError(id: string, message: string): void {
  const box = $(id);
  box.hidden = !message;
  box.textContent = message;
}

async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${SERVER}${path}`, { ...init, headers: { "content-type": "application/json" } });
  } catch {
    throw new Error("Can't reach the agent server. Start it, then try again.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    const text = Array.isArray(detail) ? detail.map((d: any) => String(d.msg ?? d).replace(/^Value error, /, "")).join("; ") : detail;
    throw new Error(text || "The agent server refused.");
  }
  return body;
}

function avatarEl(avatar: string, colour: string, size = ""): HTMLElement {
  const box = document.createElement("span");
  box.className = `avatar ${size} colour-${colour}`.replace(/\s+/g, " ");
  const image = document.createElement("img");
  image.src = `avatars/${avatar}.svg`;
  image.alt = "";
  box.append(image);
  return box;
}

function setAvatar(id: string, agent: { avatar: string; colour: string }): void {
  const box = $(id);
  box.className = `avatar large colour-${agent.colour}`;
  const image = document.createElement("img");
  image.src = `avatars/${agent.avatar}.svg`;
  image.alt = "";
  box.replaceChildren(image);
}

/** For display only. It cuts rather than rounds, so it never shows more than there is. */
export function twoDecimals(value: string): string {
  const [whole, fraction = ""] = value.split(".");
  return `${whole}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

/** Why an agent has no budget to show right now, or null when it does. */
function walletNote(agent: Agent): { text: string; attention: boolean } | null {
  if (SETTING_UP.has(agent.wallet_status)) return { text: `Setting up its wallet: ${agent.wallet_detail || "starting"}…`, attention: false };
  if (agent.wallet_status === "needs_funds") return { text: "Its wallet needs USDC", attention: true };
  if (agent.wallet_status === "failed") return { text: "Wallet setup needs attention", attention: true };
  if (!agent.wallet || agent.wallet.unavailable || agent.wallet.budget_left === undefined) return { text: "Budget unavailable right now", attention: false };
  return null;
}

/** A budget as a large number with its unit beside it. The exact amount stays in the tooltip. */
function budgetEl(agent: Agent, unit: string): HTMLElement {
  const box = document.createElement("span");
  box.className = "budget-figure";
  box.title = `${agent.wallet!.budget_left} ${agent.wallet!.symbol ?? "USDC"}`;
  const amount = document.createElement("span");
  amount.className = "amount";
  amount.textContent = twoDecimals(agent.wallet!.budget_left!);
  const label = document.createElement("span");
  label.className = "unit";
  label.textContent = `${agent.wallet!.symbol ?? "USDC"} ${unit}`;
  box.append(amount, " ", label);
  return box;
}

// ---- Agents -------------------------------------------------------------------------------------

async function showHome(): Promise<void> {
  show("home");
  setError("home-error", "");
  let agents: Agent[] = [];
  try {
    agents = await api<Agent[]>("/agents");
  } catch (e) {
    setError("home-error", (e as Error).message);
    pollTimer = window.setTimeout(showHomeIfVisible, POLL_MS * 2);
    return;
  }
  renderCards(agents);
  if (agents.some((a) => SETTING_UP.has(a.wallet_status))) pollTimer = window.setTimeout(showHomeIfVisible, POLL_MS);
}

function showHomeIfVisible(): void {
  if (!$("home-view").hidden) void showHome();
}

function renderCards(agents: Agent[]): void {
  const list = $("agents");
  list.replaceChildren();
  if (!agents.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "No agents yet. Make your first one.";
    list.append(empty);
  }
  for (const agent of agents) {
    const item = document.createElement("li");
    const card = document.createElement("button");
    card.type = "button";
    card.className = "agent-card";
    const text = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = agent.name;
    const summary = document.createElement("span");
    summary.className = "summary";
    summary.textContent = agent.description;
    text.className = "card-text";
    text.append(name, summary);
    card.append(avatarEl(agent.avatar, agent.colour), text);
    const note = walletNote(agent);
    if (note) {
      const budget = document.createElement("span");
      budget.className = note.attention ? "card-note attention" : "card-note";
      budget.textContent = note.text;
      card.append(budget);
    } else {
      card.append(budgetEl(agent, "left"));
    }
    card.addEventListener("click", () => void openAgent(agent.id));
    item.append(card);
    list.append(item);
  }
}

// ---- One agent ----------------------------------------------------------------------------------

async function openAgent(id: string, keepPrompt = false): Promise<void> {
  try {
    current = await api<Agent>(`/agents/${id}`);
  } catch (e) {
    setError("home-error", (e as Error).message);
    return;
  }
  const agent = current;
  show("agent");
  setAvatar("agent-avatar", agent);
  $("agent-name").textContent = agent.name;
  $("agent-description").textContent = agent.description;
  $("prompt-label").textContent = `What should ${agent.name} do?`;
  const limit = $<HTMLInputElement>("limit");
  limit.max = String(agent.per_order_max_inr);
  limit.value = String(agent.per_order_max_inr);
  if (!keepPrompt) $<HTMLTextAreaElement>("prompt").value = "";
  setError("prompt-error", "");
  renderWallet(agent);
  void renderHistory(agent.id);
  if (SETTING_UP.has(agent.wallet_status)) pollTimer = window.setTimeout(() => void refreshAgent(agent.id), POLL_MS);
}

async function refreshAgent(id: string): Promise<void> {
  if ($("agent-view").hidden || current?.id !== id) return;
  try {
    current = await api<Agent>(`/agents/${id}`);
    renderWallet(current);
  } catch {
    // The next poll tries again.
  }
  if (current && SETTING_UP.has(current.wallet_status)) pollTimer = window.setTimeout(() => void refreshAgent(id), POLL_MS);
}

function renderWallet(agent: Agent): void {
  const box = $("agent-wallet");
  box.replaceChildren();
  const needsAction = agent.wallet_status === "needs_funds" || agent.wallet_status === "failed";
  box.className = needsAction ? "wallet attention" : "wallet";
  const note = walletNote(agent);
  if (note) {
    const line = document.createElement("div");
    line.className = "wallet-note";
    line.textContent = note.text;
    box.append(line);
  } else {
    box.append(budgetEl(agent, "budget left"));
  }
  if (agent.wallet) {
    const address = document.createElement("div");
    const link = document.createElement("a");
    link.className = "address";
    link.href = agent.wallet.explorer;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = `${agent.wallet.address.slice(0, 8)}…${agent.wallet.address.slice(-6)}`;
    link.title = agent.wallet.address;
    address.className = "wallet-line";
    address.append("Wallet ", link);
    if (agent.wallet.balance) address.append(` · ${twoDecimals(agent.wallet.balance)} ${agent.wallet.symbol ?? "USDC"} in wallet`);
    box.append(address);
    if (needsAction) {
      const full = document.createElement("div");
      full.className = "address";
      full.textContent = agent.wallet.address;
      box.append(full);
    }
  }
  if (needsAction) {
    const detail = document.createElement("div");
    detail.textContent = agent.wallet_detail;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "secondary";
    retry.textContent = "Try again";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      await api(`/agents/${agent.id}/fund`, { method: "POST" }).catch((e) => setError("prompt-error", (e as Error).message));
      void openAgent(agent.id, true);
    });
    box.append(detail, retry);
  }
  const ready = agent.wallet_status === "ready";
  const rehearse = $<HTMLInputElement>("rehearse");
  $<HTMLButtonElement>("run").disabled = !ready && !rehearse.checked;
  $("prompt-note").textContent = rehearse.checked
    ? "A rehearsal browses as usual, but can't order or pay, and doesn't use the wallet."
    : ready ? "It pays without asking, up to this limit. You can stop it at any time before it submits." : "It can pay once its wallet is ready. You can rehearse in the meantime.";
}

async function renderHistory(id: string): Promise<void> {
  const list = $("history");
  const runs = await api<any[]>(`/agents/${id}/runs`).catch(() => []);
  if (current?.id !== id) return;
  list.replaceChildren();
  if (!runs.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "Nothing yet.";
    list.append(empty);
  }
  for (const run of runs) {
    const item = document.createElement("li");
    const status = document.createElement("span");
    const name = run.status ?? "RUNNING";
    status.className = `status ${TONES[name] ?? ""}`;
    status.textContent = TITLES[name] ?? name;
    const when = new Date(run.started * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    const site = run.site ? ` · ${run.site}` : "";
    const body = document.createElement("div");
    body.className = "history-text";
    const line = document.createElement("div");
    line.className = "history-prompt";
    line.textContent = run.prompt;
    const meta = document.createElement("div");
    meta.className = "history-meta";
    meta.textContent = `${when}${site}`;
    body.append(line, meta);
    if (run.detail && name !== "PAID") {
      const detail = document.createElement("div");
      detail.className = "history-meta";
      detail.textContent = run.detail;
      body.append(detail);
    }
    const side = document.createElement("div");
    side.className = "history-side";
    if (run.amount_paise != null) {
      const money = document.createElement("span");
      money.className = "amount";
      money.textContent = `₹${run.amount_paise / 100}`;
      side.append(money);
    }
    side.append(status);
    item.append(body, side);
    list.append(item);
  }
}

// ---- Starting and watching a run ------------------------------------------------------------------

function enterRun(agent: { name: string; avatar: string; colour: string }, prompt: string): void {
  show("run");
  setAvatar("run-avatar", agent);
  $("run-name").textContent = agent.name;
  $("run-prompt").textContent = prompt;
  $("outcome").hidden = true;
  $("log").replaceChildren();
  $("shot").hidden = true;
  $("stop").hidden = false;
  $<HTMLButtonElement>("stop").disabled = false;
  $("resume").hidden = true;
  $("done").hidden = true;
  setRunState("Working…");
}

/** What the agent is doing now, or nothing once it's finished. */
function setRunState(text: string | null): void {
  $("run-state").hidden = text === null;
  if (text) $("run-state-text").textContent = text;
}

function showOutcome(status: string, message: string): void {
  const box = $("outcome");
  box.hidden = false;
  box.className = TONES[status] ?? "";
  box.textContent = `${TITLES[status] ?? status}: ${message}`;
}

let runAgent: { avatar: string; colour: string } | null = null;

function addStep(event: any): void {
  const item = document.createElement("li");
  item.dataset.stage = event.stage;
  if (runAgent) item.append(avatarEl(runAgent.avatar, runAgent.colour, "small"));
  const body = document.createElement("div");
  const stage = document.createElement("span");
  stage.className = "stage";
  stage.textContent = `${event.stage}: `;
  body.append(stage, event.message ?? "");
  if (event.model) {
    const model = document.createElement("span");
    model.className = "model";
    model.textContent = ` (${event.model})`;
    body.append(model);
  }
  item.append(body);
  $("log").prepend(item);
}

function onEvent(event: any): void {
  addStep(event);
  if (typeof event.screenshot === "string") {
    const shot = $<HTMLImageElement>("shot");
    shot.src = `data:image/jpeg;base64,${event.screenshot}`;
    shot.hidden = false;
  }
  if (event.stage === "need_help") {
    $("resume").hidden = false;
    setRunState("Waiting for you");
    const box = $("outcome");
    box.hidden = false;
    box.className = "warn";
    box.textContent = `${$("run-name").textContent} needs help: ${event.message}`;
  }
  if (event.stage === "result") {
    showOutcome(event.status, event.message);
    setRunState(null);
  }
}

function runEnded(): void {
  $("stop").hidden = true;
  $("resume").hidden = true;
  $("done").hidden = false;
  setRunState(null);
}

$<HTMLFormElement>("prompt-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current) return;
  const agent = current;
  const prompt = $<HTMLTextAreaElement>("prompt").value.trim();
  setError("prompt-error", "");
  runAgent = agent;
  enterRun(agent, prompt); // first, so the run's first steps have somewhere to appear
  const reply = await chrome.runtime.sendMessage({
    kind: "start", agentId: agent.id, agent: { name: agent.name, avatar: agent.avatar, colour: agent.colour }, prompt,
    maxInr: Number($<HTMLInputElement>("limit").value), rehearse: $<HTMLInputElement>("rehearse").checked,
  });
  if (!reply?.ok) {
    show("agent");
    setError("prompt-error", reply?.error ?? "Couldn't start the agent.");
  }
});

$("rehearse").addEventListener("change", () => {
  if (current) renderWallet(current);
});

$("stop").addEventListener("click", () => {
  $<HTMLButtonElement>("stop").disabled = true;
  setRunState("Stopping…");
  void chrome.runtime.sendMessage({ kind: "stop" });
});

$("resume").addEventListener("click", () => {
  $("resume").hidden = true;
  $("outcome").hidden = true;
  setRunState("Working…");
  void chrome.runtime.sendMessage({ kind: "resume" });
});

$("done").addEventListener("click", () => {
  if (current) void openAgent(current.id);
  else void showHome();
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.kind === "event") onEvent(message.event);
  else if (message.kind === "ended") runEnded();
  else if (message.kind === "error") showOutcome("FAILED", message.error);
});

// ---- Creating and editing ---------------------------------------------------------------------------

async function loadOptions(): Promise<Options> {
  options ??= await api<Options>("/templates");
  return options;
}

function renderPickers(): void {
  const opts = options!;
  setAvatar("avatar-preview", picked);
  const avatars = $("avatars");
  avatars.replaceChildren(...opts.avatars.map((avatar) => {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.className = "avatar-choice";
    choice.setAttribute("role", "radio");
    choice.setAttribute("aria-checked", String(avatar === picked.avatar));
    choice.setAttribute("aria-label", avatar);
    choice.append(avatarEl(avatar, picked.colour));
    choice.addEventListener("click", () => {
      picked.avatar = avatar;
      renderPickers();
    });
    return choice;
  }));
  const colours = $("colours");
  colours.replaceChildren(...opts.colours.map((colour) => {
    const swatch = document.createElement("button");
    swatch.type = "button";
    swatch.className = `swatch colour-${colour}`;
    swatch.setAttribute("role", "radio");
    swatch.setAttribute("aria-checked", String(colour === picked.colour));
    swatch.setAttribute("aria-label", colour);
    swatch.addEventListener("click", () => {
      picked.colour = colour;
      renderPickers();
    });
    return swatch;
  }));
}

function fillForm(source: any): void {
  picked = { avatar: source.avatar, colour: source.colour };
  $<HTMLInputElement>("name").value = source.name ?? "";
  $<HTMLInputElement>("description").value = source.description ?? "";
  $<HTMLTextAreaElement>("instructions").value = source.instructions ?? "";
  const profile = $<HTMLTextAreaElement>("profile");
  profile.value = source.profile ?? "";
  profile.placeholder = source.profile_hint ?? "";
  $<HTMLTextAreaElement>("sites").value = (source.sites ?? []).join("\n");
  $<HTMLInputElement>("start-url").value = source.start_url ?? "";
  $<HTMLInputElement>("per-order").value = String(source.per_order_max_inr ?? "");
  renderPickers();
}

async function openEditor(agent: Agent | null): Promise<void> {
  let opts: Options;
  try {
    opts = await loadOptions();
  } catch (e) {
    setError("home-error", (e as Error).message);
    return;
  }
  editing = agent;
  show("edit");
  setError("edit-error", "");
  $("edit-title").textContent = agent ? `Edit ${agent.name}` : "New agent";
  $("template-field").hidden = !!agent;
  $("budget-field").hidden = !!agent;
  $<HTMLInputElement>("budget").required = !agent;
  $("manage").hidden = !agent;
  $<HTMLInputElement>("per-order").max = String(opts.global_max_inr);
  $<HTMLInputElement>("budget").max = opts.max_budget_usdc;
  $<HTMLInputElement>("new-budget").max = opts.max_budget_usdc;
  $<HTMLInputElement>("save").textContent = agent ? "Save" : "Create agent";
  if (agent) {
    fillForm(agent);
    $<HTMLInputElement>("new-budget").value = agent.budget_usdc;
    return;
  }
  const select = $<HTMLSelectElement>("template");
  select.replaceChildren(...opts.templates.map((t) => {
    const option = document.createElement("option");
    option.value = t.id;
    option.textContent = t.name || "Blank";
    return option;
  }));
  applyTemplate();
}

function applyTemplate(): void {
  const template = options!.templates.find((t) => t.id === $<HTMLSelectElement>("template").value);
  fillForm(template);
  $<HTMLInputElement>("budget").value = template.budget_usdc;
}

$("template").addEventListener("change", applyTemplate);
$("new-agent").addEventListener("click", () => void openEditor(null));
$("edit-agent").addEventListener("click", () => {
  if (current) void openEditor(current);
});
$("edit-back").addEventListener("click", () => {
  if (editing) void openAgent(editing.id);
  else void showHome();
});
for (const back of document.querySelectorAll<HTMLElement>("[data-to=home]")) back.addEventListener("click", () => void showHome());

$<HTMLFormElement>("agent-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("edit-error", "");
  const save = $<HTMLButtonElement>("save");
  const fields = {
    name: $<HTMLInputElement>("name").value.trim(),
    description: $<HTMLInputElement>("description").value.trim(),
    avatar: picked.avatar,
    colour: picked.colour,
    instructions: $<HTMLTextAreaElement>("instructions").value.trim(),
    profile: $<HTMLTextAreaElement>("profile").value.trim(),
    sites: $<HTMLTextAreaElement>("sites").value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean),
    start_url: $<HTMLInputElement>("start-url").value.trim(),
    per_order_max_inr: Number($<HTMLInputElement>("per-order").value),
  };
  save.disabled = true;
  try {
    const saved = editing
      ? await api<Agent>(`/agents/${editing.id}`, { method: "PATCH", body: JSON.stringify(fields) })
      : await api<Agent>("/agents", { method: "POST", body: JSON.stringify({ ...fields, budget_usdc: $<HTMLInputElement>("budget").value }) });
    await openAgent(saved.id);
  } catch (e) {
    setError("edit-error", (e as Error).message);
  } finally {
    save.disabled = false;
  }
});

$<HTMLFormElement>("budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editing) return;
  setError("edit-error", "");
  try {
    await api(`/agents/${editing.id}/budget`, { method: "POST", body: JSON.stringify({ usdc: $<HTMLInputElement>("new-budget").value }) });
    await openAgent(editing.id);
  } catch (e) {
    setError("edit-error", (e as Error).message);
  }
});

$("archive").addEventListener("click", async () => {
  if (!editing || !confirm(`Archive ${editing.name}? Its budget is revoked on-chain.`)) return;
  const button = $<HTMLButtonElement>("archive");
  button.disabled = true;
  setError("edit-error", "");
  try {
    await api(`/agents/${editing.id}`, { method: "DELETE" });
    current = null;
    await showHome();
  } catch (e) {
    setError("edit-error", (e as Error).message);
  } finally {
    button.disabled = false;
  }
});

// ---- Opening the panel ------------------------------------------------------------------------------

chrome.runtime.sendMessage({ kind: "status" }).then((status) => {
  if (status?.running && status.agent) {
    runAgent = status.agent;
    enterRun(status.agent, status.prompt ?? "");
    for (const event of status.history ?? []) onEvent(event);
    return;
  }
  void showHome();
}).catch(() => void showHome());

export {};
