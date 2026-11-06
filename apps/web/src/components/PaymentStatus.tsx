import CheckCircleOutline from "@mui/icons-material/CheckCircleOutlined";
import ErrorOutline from "@mui/icons-material/ErrorOutlined";
import InfoOutlined from "@mui/icons-material/InfoOutlined";
import OpenInNew from "@mui/icons-material/OpenInNew";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { useCallback, useEffect, useState } from "react";
import { api, type AppConfig, type OfframpView } from "../lib/api";
import { describeStatus, formatDateTime, formatInr, formatUnits, type Tone } from "../lib/format";
import { ensureChain, signTypedData, walletErrorMessage, type Eip1193Provider } from "../lib/wallet";
import { GroupedBlock, GroupedList } from "./Grouped";

const POLL_MS = 3000;
const DISPUTE_SIGNATURE_TTL_SEC = 600;

function ToneIcon({ tone }: { tone: Tone }) {
  if (tone === "progress") return <CircularProgress size={28} aria-hidden />;
  if (tone === "success") return <CheckCircleOutline color="success" fontSize="large" />;
  if (tone === "error") return <ErrorOutline color="error" fontSize="large" />;
  if (tone === "warning") return <InfoOutlined color="warning" fontSize="large" />;
  return <InfoOutlined color="action" fontSize="large" />;
}

interface Props {
  config: AppConfig;
  provider: Eip1193Provider;
  address: string;
  id: string;
  viewToken?: string;
  /** Payee from this device's records, when the backend wasn't given a token. */
  fallbackPayee?: { upiId: string; name: string | null };
  onChanged?: () => void;
}

export default function PaymentStatus({ config, provider, address, id, viewToken, fallbackPayee, onChanged }: Props) {
  const [view, setView] = useState<OfframpView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [disputing, setDisputing] = useState<string | null>(null);
  const [disputeError, setDisputeError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.offramp(id, viewToken);
      setView(next);
      setLoadError(null);
      return next;
    } catch (e) {
      setLoadError((e as Error).message);
      return null;
    }
  }, [id, viewToken]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const loop = async () => {
      const next = await refresh();
      if (!stopped && (next === null || describeStatus(next).active)) timer = window.setTimeout(() => void loop(), POLL_MS);
    };
    void loop();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [refresh, view?.status]);

  if (!view) {
    return loadError
      ? <Alert severity="error" role="alert">{loadError}</Alert>
      : <Stack sx={{ alignItems: "center", py: 6 }}><CircularProgress aria-label="Loading" /></Stack>;
  }

  const chain = config.chains[view.chain];
  const status = describeStatus(view);
  const payee = view.payee ?? fallbackPayee ?? null;
  const ownPayment = view.user?.toLowerCase() === address.toLowerCase();
  const txs = [
    ["Lock", view.txs.lock],
    ["Completion", view.txs.complete],
    ["Dispute", view.txs.dispute],
    ["Final", view.txs.final],
  ].filter((row): row is [string, string] => !!row[1]);

  const dispute = async () => {
    setDisputeError(null);
    try {
      setDisputing("Switching network in your wallet…");
      await ensureChain(provider, view.chain, chain);
      setDisputing("Sign the dispute in your wallet. It's free.");
      const deadline = String(Math.floor(Date.now() / 1000) + DISPUTE_SIGNATURE_TTL_SEC);
      const signature = await signTypedData(provider, address, {
        domain: chain.vaultDomain,
        types: { EIP712Domain: chain.types.EIP712Domain, DisputeIntent: chain.types.DisputeIntent },
        primaryType: "DisputeIntent",
        message: { id: view.id, user: address, deadline },
      });
      setDisputing("Sending your dispute…");
      await api.dispute(view.chain, view.id, deadline, signature);
      setConfirming(false);
      await refresh();
      onChanged?.();
    } catch (e) {
      setDisputeError(e instanceof Error && "status" in e ? e.message : walletErrorMessage(e));
    } finally {
      setDisputing(null);
    }
  };

  return (
    <Stack spacing={2}>
      <GroupedList>
        <GroupedBlock>
          <Stack direction="row" spacing={2} aria-live="polite" sx={{ alignItems: "flex-start" }}>
            <span key={status.tone} className="rr-status-change"><ToneIcon tone={status.tone} /></span>
            <div key={status.title} className="rr-status-text">
              <Typography variant="h6" component="h2">{status.title}</Typography>
              <Typography variant="body2" color="text.secondary">{status.detail}</Typography>
            </div>
          </Stack>
        </GroupedBlock>
        <GroupedBlock>
          <Stack spacing={1}>
            <div>
              <Typography variant="h3" component="p">{formatInr(view.inrPaise)}</Typography>
              <Typography color="text.secondary" sx={{ mt: 0.5, overflowWrap: "anywhere" }}>
                {payee ? `to ${payee.name ? `${payee.name} · ` : ""}${payee.upiId}` : "UPI payment"}
              </Typography>
            </div>
            {chain && view.amount && (
              <Typography variant="body2" color="text.secondary">
                {formatUnits(view.amount, chain.token.decimals)} {chain.token.symbol} on {chain.name}
              </Typography>
            )}
            {view.status === "DISPUTED" && view.timers.disputeDeadline && (
              <Typography variant="body2" color="text.secondary">
                Refunded automatically after {formatDateTime(view.timers.disputeDeadline)} if not resolved.
              </Typography>
            )}
          </Stack>
        </GroupedBlock>
        {chain && txs.length > 0 && (
          <GroupedBlock>
            <Stack direction="row" spacing={2} useFlexGap sx={{ flexWrap: "wrap", my: -1 }}>
              {txs.map(([label, hash]) => (
                <Link key={label} href={`${chain.explorer}/tx/${hash}`} target="_blank" rel="noopener noreferrer" variant="body2" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, py: 1 }}>
                  {label} <OpenInNew sx={{ fontSize: 14 }} />
                </Link>
              ))}
            </Stack>
          </GroupedBlock>
        )}
      </GroupedList>

      {view.canDispute && ownPayment && (
        <Stack spacing={0.5}>
          <Button color="error" variant="outlined" onClick={() => { setDisputeError(null); setConfirming(true); }}>
            Merchant wasn't paid? Dispute
          </Button>
          {view.timers.releasableAt && (
            <Typography variant="caption" color="text.secondary" sx={{ textAlign: "center" }}>
              You can dispute until {formatDateTime(view.timers.releasableAt)}
            </Typography>
          )}
        </Stack>
      )}

      <Dialog open={confirming} onClose={() => !disputing && setConfirming(false)}>
        <DialogTitle>Dispute this payment?</DialogTitle>
        <DialogContent>
          <Stack spacing={2}>
            <DialogContentText>
              This freezes the funds while it's reviewed. If it isn't resolved within 7 days, they're returned to you. You don't pay any gas.
            </DialogContentText>
            {disputeError && <Alert severity="error" role="alert">{disputeError}</Alert>}
            {disputing && (
              <Stack direction="row" spacing={1.5} role="status" sx={{ alignItems: "center" }}>
                <CircularProgress size={20} />
                <Typography variant="body2">{disputing}</Typography>
              </Stack>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button disabled={!!disputing} onClick={() => setConfirming(false)}>Cancel</Button>
          <Button color="error" disabled={!!disputing} onClick={dispute}>Dispute</Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
