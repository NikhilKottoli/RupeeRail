import ImageOutlined from "@mui/icons-material/ImageOutlined";
import QrCodeScanner from "@mui/icons-material/QrCodeScanner";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import InputAdornment from "@mui/material/InputAdornment";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";
import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import { api, type Account, type AppConfig, type Quote } from "../lib/api";
import { formatInr } from "../lib/format";
import { rememberPayment } from "../lib/localPayments";
import { decodeImageFile } from "../lib/qrImage";
import { paiseToRupeesString, parseUpiQr, QrError, rupeesToPaise, type ParsedQr } from "../lib/upi";
import { ensureChain, signTypedData, splitSignature, walletErrorMessage, type Eip1193Provider } from "../lib/wallet";
import PaymentStatus from "./PaymentStatus";
import ScanDialog from "./ScanDialog";

interface Props {
  config: AppConfig;
  provider: Eip1193Provider;
  address: string;
}

type Stage = "scan" | "details" | "review" | "status";
const STAGE_ORDER: Stage[] = ["scan", "details", "review", "status"];

function Row({ label, value }: { label: string; value: string }) {
  return (
    <Stack direction="row" sx={{ justifyContent: "space-between", gap: 2 }}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography variant="body2" sx={{ textAlign: "right", overflowWrap: "anywhere" }}>{value}</Typography>
    </Stack>
  );
}

export default function PayScreen({ config, provider, address }: Props) {
  const chainKeys = Object.keys(config.chains);
  const [stage, setStage] = useState<Stage>("scan");
  const [scanning, setScanning] = useState(false);
  const [showPaste, setShowPaste] = useState(false);
  const [pasted, setPasted] = useState("");
  const [qr, setQr] = useState<ParsedQr | null>(null);
  const [inr, setInr] = useState("");
  const [chainKey, setChainKey] = useState(chainKeys[0]);
  const [account, setAccount] = useState<Account | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [paymentId, setPaymentId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const previousStage = useRef(stage);
  const direction = STAGE_ORDER.indexOf(stage) < STAGE_ORDER.indexOf(previousStage.current) ? "back" : "forward";
  useEffect(() => {
    previousStage.current = stage;
  }, [stage]);
  const stageProps = { className: "rr-stage", "data-direction": direction };

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

  useEffect(() => {
    if (stage !== "details") return;
    let current = true;
    setAccount(null);
    api.account(chainKey, address).then((a) => current && setAccount(a)).catch(() => undefined);
    return () => {
      current = false;
    };
  }, [stage, chainKey, address]);

  useEffect(() => {
    if (stage !== "review" || !quote) return;
    const tick = () => setSecondsLeft(Math.max(0, Number(quote.display.expiresAt) - Math.floor(Date.now() / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [stage, quote]);

  const acceptQr = useCallback((text: string) => {
    setScanning(false);
    try {
      const parsed = parseUpiQr(text);
      setQr(parsed);
      setInr(parsed.amountPaise !== null ? paiseToRupeesString(parsed.amountPaise) : "");
      setError(null);
      setPasted("");
      setShowPaste(false);
      setStage("details");
    } catch (e) {
      setError(e instanceof QrError ? e.message : "That QR code couldn't be read.");
    }
  }, []);

  const onFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError(null);
    setBusy("Reading the image…");
    try {
      const text = await decodeImageFile(file);
      if (text) acceptQr(text);
      else setError("No QR code found in that image. Try a clearer photo.");
    } catch {
      setError("That image couldn't be opened.");
    } finally {
      setBusy(null);
    }
  };

  const getQuote = async () => {
    if (!qr || paise === null || amountError) return;
    setError(null);
    setBusy("Getting the best rate…");
    try {
      setQuote(await api.quote(chainKey, address, paiseToRupeesString(paise), qr.qrText));
      setStage("review");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const pay = async () => {
    if (!quote) return;
    setError(null);
    try {
      setBusy("Switching network in your wallet…");
      await ensureChain(provider, chainKey, chain);
      setBusy(`Step 1 of 2: approve ${quote.display.symbol} in your wallet`);
      const permitSignature = await signTypedData(provider, address, quote.signing.permit);
      setBusy("Step 2 of 2: confirm the payment in your wallet");
      const userSig = await signTypedData(provider, address, quote.signing.intent);
      const { r, s, v } = splitSignature(permitSignature);
      setBusy("Sending your payment…");
      rememberPayment(address, quote.id, {
        viewToken: quote.viewToken, upiId: quote.payee.upiId, name: quote.payee.name, createdAt: Date.now(),
      });
      const deadline = String(quote.signing.permit.message.deadline);
      await api.submit(chainKey, quote.intent, userSig, { deadline, v, r, s });
      setPaymentId(quote.id);
      setStage("status");
    } catch (e) {
      setError(e instanceof Error && "status" in e ? e.message : walletErrorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const reset = () => {
    setQr(null);
    setQuote(null);
    setPaymentId(null);
    setError(null);
    setInr("");
    setStage("scan");
  };

  const errorAlert = error && <Alert severity="error" role="alert">{error}</Alert>;
  const busyLine = busy && (
    <Stack direction="row" spacing={1.5} role="status" sx={{ alignItems: "center" }}>
      <CircularProgress size={20} />
      <Typography variant="body2">{busy}</Typography>
    </Stack>
  );

  if (stage === "status" && paymentId && quote) {
    return (
      <Stack key="status" spacing={2} {...stageProps}>
        <PaymentStatus config={config} provider={provider} address={address} id={paymentId} viewToken={quote.viewToken} />
        <Button variant="outlined" onClick={reset}>New payment</Button>
      </Stack>
    );
  }

  if (stage === "review" && quote) {
    const expired = secondsLeft <= 0;
    const tooLow = account !== null && BigInt(account.balance) < BigInt(quote.intent.amount);
    return (
      <Stack key="review" spacing={2} {...stageProps}>
        <Typography variant="h5" component="h2">Review payment</Typography>
        <Card>
          <CardContent>
            <Stack spacing={1.5}>
              <div>
                <Typography variant="h4" component="p">{formatInr(paise)}</Typography>
                <Typography color="text.secondary" sx={{ overflowWrap: "anywhere" }}>
                  to {quote.payee.name ? `${quote.payee.name} · ` : ""}{quote.payee.upiId}
                </Typography>
              </div>
              <Divider />
              <Row label="You pay" value={`${quote.display.amount} ${quote.display.symbol}`} />
              <Row label="Network" value={chain.name} />
              <Row label="Rate" value={`₹${quote.display.rate} per ${quote.display.symbol}`} />
              <Row label="Market rate" value={`₹${quote.display.marketRate}`} />
              <Row label="Network fee" value={quote.display.networkFee} />
            </Stack>
          </CardContent>
        </Card>
        {tooLow && <Alert severity="warning">Your wallet doesn't have enough {quote.display.symbol} for this payment.</Alert>}
        {errorAlert}
        {busyLine}
        {expired ? (
          <Button variant="contained" size="large" disabled={!!busy} onClick={getQuote}>Get a new rate</Button>
        ) : (
          <Button variant="contained" size="large" disabled={!!busy || tooLow} onClick={pay}>
            Pay · rate held for {secondsLeft}s
          </Button>
        )}
        <Button disabled={!!busy} onClick={() => { setError(null); setStage("details"); }}>Back</Button>
      </Stack>
    );
  }

  if (stage === "details" && qr) {
    const fixedAmount = qr.amountPaise !== null;
    return (
      <Stack key="details" spacing={2.5} {...stageProps}>
        <div>
          <Typography variant="body2" color="text.secondary">Paying</Typography>
          <Typography variant="h5" component="h2" sx={{ overflowWrap: "anywhere" }}>{qr.name ?? qr.upiId}</Typography>
          {qr.name && <Typography color="text.secondary" sx={{ overflowWrap: "anywhere" }}>{qr.upiId}</Typography>}
        </div>
        <TextField
          label="Amount"
          value={inr}
          onChange={(e) => setInr(e.target.value)}
          error={!!amountError}
          helperText={amountError ?? (fixedAmount ? "Set by this QR code" : `Up to ${formatInr(maxPaise)}`)}
          disabled={fixedAmount}
          autoFocus={!fixedAmount}
          slotProps={{
            input: { startAdornment: <InputAdornment position="start">₹</InputAdornment> },
            htmlInput: { inputMode: "decimal", autoComplete: "off" },
          }}
        />
        <div>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }} id="pay-with">Pay with</Typography>
          <ToggleButtonGroup
            exclusive
            fullWidth
            color="primary"
            value={chainKey}
            aria-labelledby="pay-with"
            onChange={(_, value) => value && setChainKey(value)}
          >
            {chainKeys.map((key) => (
              <ToggleButton key={key} value={key} sx={{ textTransform: "none", minHeight: 48, borderRadius: 3 }}>
                {config.chains[key].token.symbol} · {config.chains[key].name}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1, minHeight: 20 }}>
            {account ? `Balance: ${account.balanceFormatted} ${account.symbol}` : " "}
          </Typography>
        </div>
        {errorAlert}
        {busyLine}
        <Button variant="contained" size="large" disabled={!!busy || paise === null || !!amountError} onClick={getQuote}>
          Continue
        </Button>
        <Button disabled={!!busy} onClick={reset}>Cancel</Button>
      </Stack>
    );
  }

  return (
    <Stack key="scan" spacing={2.5} sx={{ pt: 2 }} {...stageProps}>
      <Stack spacing={1}>
        <Typography variant="h4" component="h2">Scan to pay</Typography>
        <Typography color="text.secondary">Pay any UPI QR from your stablecoin balance.</Typography>
      </Stack>
      {errorAlert}
      {busyLine}
      <Button variant="contained" size="large" startIcon={<QrCodeScanner />} onClick={() => { setError(null); setScanning(true); }}>
        Scan QR code
      </Button>
      <Button variant="outlined" size="large" startIcon={<ImageOutlined />} onClick={() => fileInput.current?.click()}>
        Upload a photo
      </Button>
      <input ref={fileInput} type="file" accept="image/*" hidden onChange={onFile} aria-label="QR code image" />
      {showPaste ? (
        <Stack spacing={1.5}>
          <TextField
            label="UPI link"
            placeholder="upi://pay?pa=…"
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            autoFocus
            slotProps={{ htmlInput: { autoCapitalize: "off", autoCorrect: "off", spellCheck: false } }}
          />
          <Button variant="outlined" disabled={!pasted.trim()} onClick={() => acceptQr(pasted)}>Continue</Button>
        </Stack>
      ) : (
        <Button onClick={() => setShowPaste(true)}>Enter a UPI link</Button>
      )}
      <ScanDialog open={scanning} onClose={() => setScanning(false)} onResult={acceptQr} />
    </Stack>
  );
}
