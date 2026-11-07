import ArrowBack from "@mui/icons-material/ArrowBack";
import ContentCopyOutlined from "@mui/icons-material/ContentCopyOutlined";
import FileDownloadOutlined from "@mui/icons-material/FileDownloadOutlined";
import HelpOutlineOutlined from "@mui/icons-material/HelpOutlineOutlined";
import OpenInNew from "@mui/icons-material/OpenInNew";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  arcAddressUrl, arcTxUrl, arkiv, avatarColour, avatarUrl, categoryName, entityUrl, publisherUrl,
  type ArkivMeta, type Outcome, type PublicAgent, type PublicReceipt,
} from "../lib/arkiv";
import { formatDateTime, formatInr, shortAddress } from "../lib/format";
import AgentsIllustration from "./AgentsIllustration";
import { GroupedBlock, GroupedButton, GroupedDetail, GroupedList, heroAction, RowAmount } from "./Grouped";

const REFRESH_MS = 60_000; // an entity reaching its expiry sends no event, so this is how the page notices
const CHANGE_DEBOUNCE_MS = 300;

/** Calls onChange when anything of ours changes on Arkiv. Returns whether the live connection is up. */
function useLiveChanges(onChange: () => void): boolean {
  const [live, setLive] = useState(false);
  const latest = useRef(onChange);
  latest.current = onChange;

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const feed = new EventSource("/arkiv/feed");
    let timer: number | undefined;
    const changed = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => latest.current(), CHANGE_DEBOUNCE_MS);
    };
    feed.addEventListener("ready", () => {
      setLive(true);
      changed(); // anything missed while disconnected
    });
    feed.addEventListener("changed", changed);
    feed.onerror = () => setLive(false);
    const refresh = window.setInterval(() => latest.current(), REFRESH_MS);
    return () => {
      feed.close();
      window.clearTimeout(timer);
      window.clearInterval(refresh);
    };
  }, []);
  return live;
}

function AgentAvatar({ agent, size = 44 }: { agent: Pick<PublicAgent, "avatar" | "colour">; size?: number }) {
  return (
    <Box sx={{ flexShrink: 0, display: "grid", placeItems: "center", width: size, height: size, borderRadius: "50%", bgcolor: avatarColour(agent.colour) }}>
      <Box component="img" src={avatarUrl(agent.avatar)} alt="" sx={{ width: size * 0.72, height: size * 0.72 }} />
    </Box>
  );
}

function Working() {
  return <Chip size="small" color="primary" label="Working now" />;
}

function LiveMark({ live }: { live: boolean }) {
  if (!live) return null;
  return (
    <Stack direction="row" spacing={0.75} sx={{ alignItems: "center", flexShrink: 0 }} role="status" aria-label="Updating live">
      <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: "success.main" }} />
      <Typography variant="caption" color="text.secondary">Live</Typography>
    </Stack>
  );
}

function ExternalLink({ href, children }: { href: string | null; children: string }) {
  if (!href) return <span>{children}</span>;
  return (
    <Link href={href} target="_blank" rel="noreferrer" underline="hover" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      {children}
      <OpenInNew sx={{ fontSize: 14 }} aria-hidden />
    </Link>
  );
}

function paidSummary(agent: PublicAgent): string {
  const { paid, paidInrPaise } = agent.stats;
  if (!paid) return "No paid orders yet";
  return `${paid} paid ${paid === 1 ? "order" : "orders"} · ${formatInr(paidInrPaise)}`;
}

function CopyableCode({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
      <Box component="code" sx={{ flexGrow: 1, minWidth: 0, p: 1.5, borderRadius: "12px", bgcolor: "background.paper", fontSize: "0.75rem", overflowWrap: "anywhere" }}>{text}</Box>
      <Tooltip title={copied ? "Copied" : "Copy"}>
        <IconButton onClick={() => void copy()} aria-label={label}><ContentCopyOutlined fontSize="small" /></IconButton>
      </Tooltip>
    </Stack>
  );
}

const EXTENSION_PACK = "/downloads/rupeerail-agents-extension.zip";

const heroButton = { ...heroAction, flex: "1 1 170px", px: "20px" };

/** The extension, packed for sideloading because it isn't in the Chrome Web Store. */
function GetTheExtension() {
  const [open, setOpen] = useState(false);

  return (
    <Stack spacing={1.5} sx={{ alignSelf: "center", width: "100%", maxWidth: 440 }} data-testid="get-the-extension">
      <Stack direction="row" useFlexGap spacing={1} sx={{ flexWrap: "wrap", justifyContent: "center" }}>
        <Button variant="contained" size="large" startIcon={<FileDownloadOutlined />} component="a" href={EXTENSION_PACK} download onClick={() => setOpen(true)} sx={heroButton}>
          Download the extension
        </Button>
        <Button variant="outlined" size="large" startIcon={<HelpOutlineOutlined />} onClick={() => setOpen((v) => !v)} aria-expanded={open} sx={heroButton}>
          {open ? "Hide the steps" : "How to install it"}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ textAlign: "center" }}>
        Agents shop in your own Chrome on a computer, through the RupeeRail extension. They run on RupeeRail's shared demo server.
      </Typography>
      {open && (
        <GroupedList aria-label="How to install the extension">
          <GroupedBlock>
            <Stack spacing={1.5}>
              <Box component="ol" sx={{ m: 0, pl: 2.5, display: "flex", flexDirection: "column", gap: 1.5, typography: "body2" }}>
                <li>Unzip the download and keep the folder, because Chrome runs the extension from it.</li>
                <li>
                  <Stack spacing={1}>
                    <span>Open this address in a new tab and turn on Developer mode:</span>
                    <CopyableCode text="chrome://extensions" label="Copy the address" />
                  </Stack>
                </li>
                <li>Choose Load unpacked, and select the unzipped folder.</li>
                <li>Click the extension's icon to open its side panel, and choose Use RupeeRail's server.</li>
                <li>Choose an agent or make one, and give it a job.</li>
              </Box>
              <Alert severity="warning">
                A run pays for real from the agent's wallet, up to its limit, so tick Rehearse to try a site first. Anyone can see, change and run the agents on the demo server, so keep personal details out. Chrome also says the extension can read every site and debug the browser. It only acts on the sites you allow an agent, and only debugs the tab of a run.
              </Alert>
            </Stack>
          </GroupedBlock>
        </GroupedList>
      )}
    </Stack>
  );
}

/** Who these records come from, and how to read them without us. */
function CheckItYourself({ meta }: { meta: ArkivMeta }) {
  const [open, setOpen] = useState(false);

  return (
    <Stack spacing={1.5} data-testid="check-it-yourself">
      <Typography variant="subtitle1" component="h3">Check this yourself</Typography>
      <Typography variant="body2" color="text.secondary">
        Only records created by <ExternalLink href={publisherUrl(meta)}>{shortAddress(meta.publisher)}</ExternalLink> count, checked against Arkiv's on-chain creator.
        {meta.foreignCards > 0 && ` ${meta.foreignCards} ${meta.foreignCards === 1 ? "card" : "cards"} from other wallets ${meta.foreignCards === 1 ? "is" : "are"} ignored.`}
      </Typography>
      <Button variant="outlined" size="small" onClick={() => setOpen((v) => !v)} aria-expanded={open} sx={{ alignSelf: "flex-start" }}>
        {open ? "Hide the queries" : "Show the queries"}
      </Button>
      {open && (
        <Stack spacing={1.5}>
          <Typography variant="body2" color="text.secondary">
            These are the queries this page runs. Anyone can run them with a free <Link href="https://hub.arkiv.network/access-keys" target="_blank" rel="noreferrer">Arkiv access key</Link>, without RupeeRail's servers.
          </Typography>
          {Object.entries(meta.queries).map(([name, query]) => (
            <Box key={name} component="code" sx={{ display: "block", p: 1.5, borderRadius: "12px", bgcolor: "background.paper", fontSize: "0.75rem", overflowWrap: "anywhere" }}>{query}</Box>
          ))}
          <CopyableCode text="ARKIV_ACCESS_KEY=<your key> npm run verify" label="Copy the command" />
          <Typography variant="caption" color="text.secondary">Run it from services/arkiv-board in the RupeeRail repo.</Typography>
        </Stack>
      )}
    </Stack>
  );
}

function Directory({ meta, onOpen, onExit }: { meta: ArkivMeta | null; onOpen: (wallet: string) => void; onExit?: () => void }) {
  const [category, setCategory] = useState<string | null>(null);
  const [agents, setAgents] = useState<PublicAgent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);

  const load = useCallback(async () => {
    const mine = ++request.current;
    try {
      const { agents: found } = await arkiv.agents(category ?? undefined);
      if (mine !== request.current) return;
      setAgents(found);
      setError(null);
    } catch (e) {
      if (mine === request.current) setError((e as Error).message);
    }
  }, [category]);

  useEffect(() => {
    setAgents(null);
    void load();
  }, [load]);

  const live = useLiveChanges(load);

  return (
    <Stack spacing={2.5} sx={{ pt: 1 }}>
      {(onExit || live) && (
        <Stack direction="row" sx={{ alignItems: "center", justifyContent: "space-between", minHeight: 40 }}>
          {onExit ? <IconButton edge="start" onClick={onExit} aria-label="Back"><ArrowBack /></IconButton> : <span />}
          <LiveMark live={live} />
        </Stack>
      )}
      <AgentsIllustration />
      <Stack spacing={1} sx={{ textAlign: "center" }}>
        <Typography variant="h4" component="h2">Agents Hub</Typography>
        <Typography color="text.secondary">A marketplace of shopping agents, each paying from its own wallet. Compare their track records, which are public and stored on Arkiv, not in our database.</Typography>
      </Stack>

      <GetTheExtension />

      <Typography variant="h6" component="h3" sx={{ pt: 1 }}>Track records</Typography>
      <Stack direction="row" role="group" aria-label="Category" sx={{ flexWrap: "wrap", gap: 1 }}>
        <Chip label="All" color={category === null ? "secondary" : "default"} variant={category === null ? "filled" : "outlined"} onClick={() => setCategory(null)} aria-pressed={category === null} />
        {(meta?.categories ?? []).map((name) => (
          <Chip key={name} label={categoryName(name)} color={category === name ? "secondary" : "default"} variant={category === name ? "filled" : "outlined"} onClick={() => setCategory(name)} aria-pressed={category === name} />
        ))}
      </Stack>

      {error && <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => void load()}>Retry</Button>}>{error}</Alert>}

      {agents === null && !error && <Box sx={{ display: "grid", placeItems: "center", py: 6 }}><CircularProgress aria-label="Loading" /></Box>}
      {agents?.length === 0 && (
        <GroupedList><GroupedBlock><Typography color="text.secondary">{category ? `No ${categoryName(category).toLowerCase()} agents have published a record.` : "No agents have published a record yet."}</Typography></GroupedBlock></GroupedList>
      )}
      {!!agents?.length && (
        <GroupedList aria-label="Public agents">
          {agents.map((agent) => (
            <GroupedButton key={agent.key} onClick={() => onOpen(agent.wallet)}>
              <AgentAvatar agent={agent} />
              <Stack sx={{ flexGrow: 1, minWidth: 0 }}>
                <Typography sx={{ fontWeight: 500, overflowWrap: "anywhere" }}>{agent.name}</Typography>
                <Typography variant="body2" color="text.secondary">{categoryName(agent.category)} · {paidSummary(agent)}</Typography>
              </Stack>
              {agent.working && <Working />}
            </GroupedButton>
          ))}
        </GroupedList>
      )}

      {meta && <GroupedList><GroupedBlock><CheckItYourself meta={meta} /></GroupedBlock></GroupedList>}
    </Stack>
  );
}

const OUTCOME_TEXT: Record<Outcome, { label: string; color: "success" | "error" | "warning" }> = {
  paid: { label: "Paid", color: "success" },
  not_paid: { label: "Not paid", color: "error" },
  unknown: { label: "Unclear", color: "warning" },
};
const OUTCOME_FILTERS: { value: Outcome | null; label: string }[] = [{ value: null, label: "All" }, { value: "paid", label: "Paid" }, { value: "not_paid", label: "Not paid" }, { value: "unknown", label: "Unclear" }];
const AMOUNT_FILTERS: { value: number | null; label: string }[] = [{ value: null, label: "Any amount" }, { value: 100, label: "₹100+" }, { value: 500, label: "₹500+" }];
const TIME_FILTERS: { value: number | null; label: string }[] = [{ value: null, label: "Any time" }, { value: 1, label: "Today" }, { value: 7, label: "7 days" }];

function FilterRow<T>({ label, options, value, onChange }: { label: string; options: { value: T | null; label: string }[]; value: T | null; onChange: (value: T | null) => void }) {
  return (
    <Stack direction="row" role="group" aria-label={label} sx={{ flexWrap: "wrap", gap: 1 }}>
      {options.map((option) => {
        const selected = option.value === value;
        return <Chip key={option.label} size="small" label={option.label} color={selected ? "secondary" : "default"} variant={selected ? "filled" : "outlined"} onClick={() => onChange(option.value)} aria-pressed={selected} />;
      })}
    </Stack>
  );
}

function ReceiptRow({ receipt, explorer }: { receipt: PublicReceipt; explorer: string }) {
  const outcome = OUTCOME_TEXT[receipt.outcome] ?? OUTCOME_TEXT.unknown;
  return (
    <GroupedBlock>
      <Stack spacing={1} className="rr-arrive" data-testid="receipt">
        <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center", gap: 1 }}>
          <RowAmount>{formatInr(receipt.inrPaise)}</RowAmount>
          <Chip size="small" variant="outlined" color={outcome.color} label={outcome.label} />
        </Stack>
        <Typography variant="body2" color="text.secondary">{formatDateTime(receipt.finishedAt / 1000)}</Typography>
        <Stack direction="row" sx={{ flexWrap: "wrap", columnGap: 2, rowGap: 0.5 }}>
          <Typography variant="body2" component="span"><ExternalLink href={arcTxUrl(receipt)}>Lock on Arc</ExternalLink></Typography>
          <Typography variant="body2" component="span"><ExternalLink href={entityUrl(explorer, receipt.key)}>Arkiv record</ExternalLink></Typography>
        </Stack>
      </Stack>
    </GroupedBlock>
  );
}

function Detail({ wallet, meta, onBack }: { wallet: string; meta: ArkivMeta | null; onBack: () => void }) {
  const [agent, setAgent] = useState<PublicAgent | null>(null);
  const [missing, setMissing] = useState(false);
  const [receipts, setReceipts] = useState<PublicReceipt[] | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [minInr, setMinInr] = useState<number | null>(null);
  const [days, setDays] = useState<number | null>(null);
  const request = useRef(0);
  const explorer = meta?.explorer ?? "https://rabdi.explorer.arkiv.network";

  const load = useCallback(async () => {
    const mine = ++request.current;
    const filters = { outcome: outcome ?? undefined, minInr: minInr ?? undefined, days: days ?? undefined };
    try {
      const [card, page] = await Promise.all([arkiv.agent(wallet), arkiv.receipts(wallet, filters)]);
      if (mine !== request.current) return;
      setAgent(card);
      setMissing(false);
      setReceipts(page.items);
      setNext(page.next);
      setError(null);
    } catch (e) {
      if (mine !== request.current) return;
      if ((e as { status?: number }).status === 404) setMissing(true);
      else setError((e as Error).message);
    }
  }, [wallet, outcome, minInr, days]);

  useEffect(() => {
    setReceipts(null);
    void load();
  }, [load]);

  const live = useLiveChanges(load);

  const loadMore = async () => {
    if (next === null) return;
    const mine = request.current;
    setLoadingMore(true);
    try {
      const page = await arkiv.receipts(wallet, { outcome: outcome ?? undefined, minInr: minInr ?? undefined, days: days ?? undefined, before: next });
      if (mine !== request.current) return; // the filters changed, or newer receipts arrived
      setReceipts((rows) => [...(rows ?? []), ...page.items]);
      setNext(page.next);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
        <IconButton edge="start" onClick={onBack} aria-label="All agents"><ArrowBack /></IconButton>
        <Box sx={{ flexGrow: 1 }} />
        <LiveMark live={live} />
      </Stack>

      {error && <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => void load()}>Retry</Button>}>{error}</Alert>}
      {missing && <Alert severity="info">This wallet has no public record. Its owner may have withdrawn it, or it may have expired.</Alert>}
      {!agent && !missing && !error && <Box sx={{ display: "grid", placeItems: "center", py: 6 }}><CircularProgress aria-label="Loading" /></Box>}

      {agent && !missing && (
        <>
          <Stack direction="row" spacing={2} sx={{ alignItems: "center" }}>
            <AgentAvatar agent={agent} size={64} />
            <Stack sx={{ minWidth: 0, flexGrow: 1 }}>
              <Typography variant="h5" component="h2" sx={{ overflowWrap: "anywhere" }}>{agent.name}</Typography>
              <Typography color="text.secondary">{categoryName(agent.category)}</Typography>
            </Stack>
            {agent.working && <Working />}
          </Stack>

          <GroupedList aria-label="Track record">
            <GroupedBlock>
              <Typography variant="body2" color="text.secondary">Paid to merchants</Typography>
              <Typography variant="h4" component="p" sx={{ fontWeight: 500, letterSpacing: "-0.02em" }} data-testid="paid-total">{formatInr(agent.stats.paidInrPaise)}</Typography>
            </GroupedBlock>
            <GroupedDetail label="Paid orders" value={String(agent.stats.paid)} />
            <GroupedDetail label="Not paid" value={String(agent.stats.notPaid)} />
            {agent.stats.unknown > 0 && <GroupedDetail label="Unclear" value={String(agent.stats.unknown)} />}
            <GroupedDetail label="Listed until" value={new Date(agent.expiresAt).toLocaleDateString("en-IN", { dateStyle: "medium" })} />
          </GroupedList>
          <Typography variant="body2" color="text.secondary">
            Each paid order renews this listing for {meta?.lifetimes.cardDays ?? 30} days, so an agent that stops paying drops off by itself.
          </Typography>

          <Typography variant="h6" component="h3">Receipts</Typography>
          <Stack spacing={1}>
            <FilterRow label="Outcome" options={OUTCOME_FILTERS} value={outcome} onChange={setOutcome} />
            <FilterRow label="Amount" options={AMOUNT_FILTERS} value={minInr} onChange={setMinInr} />
            <FilterRow label="Time" options={TIME_FILTERS} value={days} onChange={setDays} />
          </Stack>

          {receipts === null && !error && <Box sx={{ display: "grid", placeItems: "center", py: 4 }}><CircularProgress aria-label="Loading receipts" /></Box>}
          {receipts?.length === 0 && <GroupedList><GroupedBlock><Typography color="text.secondary">No receipts match.</Typography></GroupedBlock></GroupedList>}
          {!!receipts?.length && (
            <GroupedList aria-label="Receipts">
              {receipts.map((receipt) => <ReceiptRow key={receipt.key} receipt={receipt} explorer={explorer} />)}
            </GroupedList>
          )}
          {next !== null && <Button variant="outlined" disabled={loadingMore} onClick={() => void loadMore()} sx={{ alignSelf: "center" }}>{loadingMore ? "Loading…" : "Load more"}</Button>}

          <GroupedList aria-label="How to check this">
            <GroupedBlock>
              <Stack spacing={1}>
                <Typography variant="subtitle1" component="h3">What this proves</Typography>
                <Typography variant="body2" color="text.secondary">
                  Spending is on Arc, from <ExternalLink href={arcAddressUrl(agent.wallet)}>{shortAddress(agent.wallet)}</ExternalLink>, and anyone can check it. RupeeRail publishes these receipts itself, and a receipt can't be edited, though the publisher could delete one. The chain can't confirm that a UPI payment arrived, so "Paid" is what the phone that paid reported.
                </Typography>
              </Stack>
            </GroupedBlock>
            {meta && <GroupedBlock><CheckItYourself meta={meta} /></GroupedBlock>}
          </GroupedList>
        </>
      )}
    </Stack>
  );
}

interface Props {
  wallet: string | null;
  onOpen: (wallet: string | null) => void;
  /** Where to go back to, when this page was reached without a wallet connected. */
  onExit?: () => void;
}

/** The public directory and each agent's track record. It needs no wallet, so anyone can open it. */
export default function AgentsScreen({ wallet, onOpen, onExit }: Props) {
  const [meta, setMeta] = useState<ArkivMeta | null>(null);

  useEffect(() => {
    // The page works without this: it only adds the categories and the publisher note.
    arkiv.meta().then(setMeta).catch(() => setMeta(null));
  }, []);

  return wallet
    ? <Detail key={wallet} wallet={wallet} meta={meta} onBack={() => onOpen(null)} />
    : <Directory meta={meta} onOpen={onOpen} onExit={onExit} />;
}
