import CheckCircleOutline from "@mui/icons-material/CheckCircleOutlined";
import ErrorOutline from "@mui/icons-material/ErrorOutlined";
import InfoOutlined from "@mui/icons-material/InfoOutlined";
import OpenInNew from "@mui/icons-material/OpenInNew";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { useCallback, useEffect, useState } from "react";
import { api, type AppConfig, type OnrampOrder } from "../lib/api";
import { formatDateTime, formatInr, shortAddress, type Tone } from "../lib/format";
import { describeOnramp } from "../lib/onramp";
import { GroupedBlock, GroupedDetail, GroupedList } from "./Grouped";

const POLL_MS = 3000;

function ToneIcon({ tone }: { tone: Tone }) {
  if (tone === "progress") return <CircularProgress size={28} aria-hidden />;
  if (tone === "success") return <CheckCircleOutline color="success" fontSize="large" />;
  if (tone === "error") return <ErrorOutline color="error" fontSize="large" />;
  if (tone === "warning") return <InfoOutlined color="warning" fontSize="large" />;
  return <InfoOutlined color="action" fontSize="large" />;
}

interface Props {
  config: AppConfig;
  orderId: string;
  /** Came back from Razorpay with a verified redirect: say "received" while the server confirms. */
  justPaid?: boolean;
  /** An order still waiting for payment can be paid from here (its link is still open). */
  showPayButton?: boolean;
}

export default function OnrampStatus({ config, orderId, justPaid = false, showPayButton = false }: Props) {
  const [order, setOrder] = useState<OnrampOrder | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.onrampOrder(orderId);
      setOrder(next);
      setLoadError(null);
      return next;
    } catch (e) {
      setLoadError((e as Error).message);
      return null;
    }
  }, [orderId]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const loop = async () => {
      const next = await refresh();
      if (!stopped && (next === null || describeOnramp(next).active)) timer = window.setTimeout(() => void loop(), POLL_MS);
    };
    void loop();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [refresh]);

  if (!order) {
    return loadError
      ? <Alert severity="error" role="alert">{loadError}</Alert>
      : <Stack sx={{ alignItems: "center", py: 6 }}><CircularProgress aria-label="Loading" /></Stack>;
  }

  const chain = config.chains[order.chain];
  const status = describeOnramp(order, justPaid);
  const final = !status.active;
  const linkOpen = order.checkout && Number(order.checkout.expiresAt) * 1000 > Date.now();

  return (
    <Stack spacing={2}>
      <GroupedList>
        <GroupedBlock>
          <Stack direction="row" spacing={2} aria-live="polite" sx={{ alignItems: "flex-start" }}>
            <ToneIcon tone={status.tone} />
            <div>
              <Typography variant="h6" component="h2">{status.title}</Typography>
              <Typography variant="body2" color="text.secondary">{status.detail}</Typography>
            </div>
          </Stack>
        </GroupedBlock>
        <GroupedBlock>
          <Typography variant="h3" component="p">{order.amount} {order.symbol}</Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>for {formatInr(order.inrPaise)}</Typography>
        </GroupedBlock>
        <GroupedDetail label="To wallet" value={shortAddress(order.wallet)} />
        <GroupedDetail label="Network" value={chain?.name ?? order.chain} />
        <GroupedDetail label="Rate" value={`₹${order.rate} per ${order.symbol ?? "token"}`} />
        {order.checkout?.paymentId && <GroupedDetail label="Razorpay receipt" value={order.checkout.paymentId} />}
        {order.status === "AWAITING_FIAT" && order.checkout && (
          <GroupedDetail label="Link expires" value={formatDateTime(order.checkout.expiresAt)} />
        )}
        {order.explorer && (
          <GroupedBlock>
            <Link href={order.explorer} target="_blank" rel="noopener noreferrer" variant="body2"
                  sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, py: 1, my: -1 }}>
              View transaction <OpenInNew sx={{ fontSize: 14 }} />
            </Link>
          </GroupedBlock>
        )}
      </GroupedList>

      {order.lastError && !final && (order.lastError.startsWith("mismatch") ? (
        <Alert severity="error">This payment didn't match the order, so no tokens were sent. Contact support with the receipt above.</Alert>
      ) : (
        <Alert severity="warning">Taking longer than usual. We keep retrying, and your payment is safe.</Alert>
      ))}
      {order.status === "FAILED" && order.lastError && <Alert severity="error">{order.lastError}</Alert>}

      {order.status === "AWAITING_FIAT" && !justPaid && order.payTo && !order.checkout && (
        <Alert severity="info">
          Pay {formatInr(order.inrPaise)} to <strong>{order.payTo.upiId}</strong> with the note <strong>{order.payTo.note}</strong>.
        </Alert>
      )}
      {showPayButton && order.status === "AWAITING_FIAT" && !justPaid && linkOpen && (
        <Button variant="contained" size="large" href={order.checkout!.url}>
          Pay {formatInr(order.inrPaise)} with Razorpay
        </Button>
      )}
    </Stack>
  );
}
