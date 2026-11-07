import ChevronRight from "@mui/icons-material/ChevronRight";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import InputAdornment from "@mui/material/InputAdornment";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";
import { useEffect, useState } from "react";
import { api, type AppConfig, type OnrampOrder } from "../lib/api";
import { formatDateTime, formatInr, shortAddress } from "../lib/format";
import { describeOnramp, recentOnramps, rememberOnramp, type LocalOnramp, type OnrampReturn } from "../lib/onramp";
import { paiseToRupeesString, rupeesToPaise } from "../lib/upi";
import { ActionRow, GroupedBlock, GroupedButton, GroupedDetail, GroupedList, heroAction } from "./Grouped";
import OnrampStatus from "./OnrampStatus";

interface Props {
  config: AppConfig;
  address: string;
  /** Set when the app was opened by Razorpay's redirect back. */
  returned?: OnrampReturn | null;
}

type Stage = "form" | "review" | "status";

function RecentPurchase({ item, onOpen }: { item: LocalOnramp; onOpen: () => void }) {
  const [order, setOrder] = useState<OnrampOrder | null>(null);
  useEffect(() => {
    let current = true;
    api.onrampOrder(item.orderId).then((o) => current && setOrder(o)).catch(() => undefined);
    return () => {
      current = false;
    };
  }, [item.orderId]);
  const primary = order ? `${order.amount} ${order.symbol ?? ""} for ${formatInr(order.inrPaise)}` : "Purchase";
  const secondary = `${order ? describeOnramp(order).title : "…"} · ${formatDateTime(Math.floor(item.createdAt / 1000))}`;
  return (
    <GroupedButton onClick={onOpen}>
      <ListItemText primary={primary} secondary={secondary} />
      <ChevronRight color="action" />
    </GroupedButton>
  );
}

export default function BuyScreen({ config, address, returned = null }: Props) {
  const chainKeys = Object.keys(config.chains);
  const [stage, setStage] = useState<Stage>(returned ? "status" : "form");
  const [chainKey, setChainKey] = useState(chainKeys[0]);
  const [inr, setInr] = useState("");
  const [order, setOrder] = useState<OnrampOrder | null>(null);
  const [statusId, setStatusId] = useState<string | null>(returned?.orderId ?? null);
  const [justPaid, setJustPaid] = useState(returned?.payment === "received");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState(() => recentOnramps(address).slice(0, 5));

  const chain = config.chains[chainKey];
  const minPaise = Number(config.limits.minInrPaise);
  const maxPaise = Number(config.limits.maxInrPaise);
  const paise = rupeesToPaise(inr);
  const amountError = inr.trim() === ""
    ? null
    : paise === null
      ? "Enter a valid amount, with up to 2 decimal places"
      : paise < minPaise || paise > maxPaise
        ? `Enter between ${formatInr(minPaise)} and ${formatInr(maxPaise)}`
        : null;

  const getRate = async () => {
    if (paise === null || amountError) return;
    setError(null);
    setBusy("Getting the best rate…");
    try {
      const created = await api.onramp(chainKey, address, paiseToRupeesString(paise));
      rememberOnramp({ orderId: created.orderId, chain: created.chain, wallet: address, createdAt: Date.now() });
      setRecent(recentOnramps(address).slice(0, 5));
      setOrder(created);
      if (created.checkout) {
        setStage("review");
      } else {
        openStatus(created.orderId);  // manual rail: the status view shows where to pay
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const payWithRazorpay = () => {
    if (!order?.checkout) return;
    setBusy("Opening Razorpay…");
    // A full-page visit (no popup or iframe): Razorpay sends the browser back to /?onramp=<id>.
    window.location.assign(order.checkout.url);
  };

  const openStatus = (orderId: string, paid = false) => {
    setStatusId(orderId);
    setJustPaid(paid);
    setStage("status");
  };

  const reset = () => {
    setOrder(null);
    setStatusId(null);
    setJustPaid(false);
    setError(null);
    setInr("");
    setRecent(recentOnramps(address).slice(0, 5));
    setStage("form");
  };

  const errorAlert = error && <Alert severity="error" role="alert">{error}</Alert>;
  const testNote = config.onramp?.testMode && (
    <Alert severity="info">Test mode: Razorpay won't charge real money. Pay with one of its test cards.</Alert>
  );
  const busyLine = busy && (
    <Stack direction="row" spacing={1.5} role="status" sx={{ alignItems: "center" }}>
      <CircularProgress size={20} />
      <Typography variant="body2">{busy}</Typography>
    </Stack>
  );

  if (stage === "status" && statusId) {
    return (
      <Stack spacing={2}>
        <OnrampStatus config={config} orderId={statusId} justPaid={justPaid} showPayButton />
        <Button variant="outlined" onClick={reset} sx={heroAction}>Buy more</Button>
      </Stack>
    );
  }

  if (stage === "review" && order?.checkout) {
    return (
      <Stack spacing={2}>
        <Typography variant="h4" component="h2">Review purchase</Typography>
        <GroupedList aria-label="Purchase details">
          <GroupedBlock>
            <Typography variant="h3" component="p">{order.amount} {order.symbol}</Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>for {formatInr(order.inrPaise)}</Typography>
          </GroupedBlock>
          <GroupedDetail label="You pay" value={formatInr(order.inrPaise)} />
          <GroupedDetail label="You get" value={`${order.amount} ${order.symbol ?? ""}`} />
          <GroupedDetail label="Rate" value={`₹${order.rate} per ${order.symbol ?? "token"}`} />
          <GroupedDetail label="Network" value={config.chains[order.chain]?.name ?? order.chain} />
          <GroupedDetail label="To wallet" value={shortAddress(order.wallet)} />
          <GroupedDetail label="Rate held until" value={formatDateTime(order.checkout.expiresAt)} />
        </GroupedList>
        {testNote}
        <Typography variant="body2" color="text.secondary">
          You'll pay on Razorpay with UPI, card or netbanking, then come back here automatically.
        </Typography>
        {errorAlert}
        {busyLine}
        <ActionRow>
          <Button variant="contained" size="large" disabled={!!busy} onClick={payWithRazorpay}>
            Pay {formatInr(order.inrPaise)} with Razorpay
          </Button>
          <Button variant="outlined" size="large" disabled={!!busy} onClick={reset}>Cancel</Button>
        </ActionRow>
      </Stack>
    );
  }

  return (
    <Stack spacing={2.5} sx={{ pt: 2 }}>
      <Stack spacing={1}>
        <Typography variant="h4" component="h2">Buy stablecoins</Typography>
        <Typography color="text.secondary">Pay in rupees and get tokens in your wallet.</Typography>
      </Stack>
      {testNote}
      <GroupedList>
        <GroupedBlock>
          <Stack spacing={2.5}>
            <TextField
              label="Amount"
              value={inr}
              onChange={(e) => setInr(e.target.value)}
              error={!!amountError}
              helperText={amountError ?? `Between ${formatInr(minPaise)} and ${formatInr(maxPaise)}`}
              slotProps={{
                input: { startAdornment: <InputAdornment position="start">₹</InputAdornment> },
                htmlInput: { inputMode: "decimal", autoComplete: "off" },
              }}
            />
            <div>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }} id="buy-token">Get</Typography>
              <ToggleButtonGroup
                exclusive
                fullWidth
                color="primary"
                value={chainKey}
                aria-labelledby="buy-token"
                onChange={(_, value) => value && setChainKey(value)}
              >
                {chainKeys.map((key) => (
                  <ToggleButton key={key} value={key}>
                    {config.chains[key].token.symbol} · {config.chains[key].name}
                  </ToggleButton>
                ))}
              </ToggleButtonGroup>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                Sent to {shortAddress(address)} on {chain.name}
              </Typography>
            </div>
          </Stack>
        </GroupedBlock>
      </GroupedList>
      {errorAlert}
      {busyLine}
      <Button variant="contained" size="large" disabled={!!busy || paise === null || !!amountError} onClick={getRate} sx={heroAction}>
        {paise !== null && !amountError ? `Continue with ${formatInr(paise)}` : "Continue"}
      </Button>

      {recent.length > 0 && (
        <Stack spacing={1}>
          <Typography variant="subtitle2" component="h3" color="text.secondary">Recent purchases</Typography>
          <GroupedList>
            {recent.map((item) => (
              <RecentPurchase key={item.orderId} item={item} onOpen={() => openStatus(item.orderId)} />
            ))}
          </GroupedList>
        </Stack>
      )}
    </Stack>
  );
}
