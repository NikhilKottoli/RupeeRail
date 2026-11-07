import ArrowBack from "@mui/icons-material/ArrowBack";
import Refresh from "@mui/icons-material/Refresh";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import IconButton from "@mui/material/IconButton";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import useMediaQuery from "@mui/material/useMediaQuery";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type AppConfig, type OfframpView } from "../lib/api";
import { describeStatus, formatDateTime, formatInr, type Tone } from "../lib/format";
import { localPayments } from "../lib/localPayments";
import type { Eip1193Provider } from "../lib/wallet";
import { GroupedButton, GroupedList, RowAmount } from "./Grouped";
import PaymentStatus from "./PaymentStatus";

const CHIP_COLOR: Record<Tone, "default" | "primary" | "success" | "warning" | "error"> = {
  progress: "primary", success: "success", warning: "warning", error: "error", neutral: "default",
};

interface Props {
  config: AppConfig;
  provider: Eip1193Provider;
  address: string;
}

export default function HistoryScreen({ config, provider, address }: Props) {
  const [items, setItems] = useState<OfframpView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const fullScreen = useMediaQuery("(max-width:600px)");
  const local = useMemo(() => localPayments(address), [address, items]); // eslint-disable-line react-hooks/exhaustive-deps

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems((await api.history(address)).items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [address]);

  useEffect(() => {
    void load();
  }, [load]);

  const record = selected ? local[selected.toLowerCase()] : undefined;

  return (
    <Stack spacing={2}>
      <Stack direction="row" sx={{ alignItems: "center", justifyContent: "space-between" }}>
        <Typography variant="h4" component="h2">History</Typography>
        <IconButton onClick={() => void load()} disabled={loading} aria-label="Refresh history"><Refresh /></IconButton>
      </Stack>
      {error && (
        <Alert severity="error" role="alert" action={<Button color="inherit" size="small" onClick={() => void load()}>Retry</Button>}>
          {error}
        </Alert>
      )}
      {items === null && !error && <Stack sx={{ alignItems: "center", py: 6 }}><CircularProgress aria-label="Loading" /></Stack>}
      {items?.length === 0 && (
        <Typography color="text.secondary" sx={{ py: 6, textAlign: "center" }}>Your payments will show up here.</Typography>
      )}
      {!!items?.length && (
        <GroupedList aria-label="Payments">
          {items.map((item) => {
            const saved = local[item.id.toLowerCase()];
            const status = describeStatus(item);
            return (
              <GroupedButton key={item.id} onClick={() => setSelected(item.id)}>
                <ListItemText
                  primary={saved?.name ?? saved?.upiId ?? "UPI payment"}
                  secondary={saved ? formatDateTime(Math.floor(saved.createdAt / 1000)) : config.chains[item.chain]?.name}
                  slotProps={{ primary: { noWrap: true } }}
                  sx={{ minWidth: 0 }}
                />
                <Stack spacing={0.75} sx={{ alignItems: "flex-end", flexShrink: 0 }}>
                  <RowAmount>{formatInr(item.inrPaise)}</RowAmount>
                  <Chip size="small" label={status.title} color={CHIP_COLOR[status.tone]} variant="outlined" />
                </Stack>
              </GroupedButton>
            );
          })}
        </GroupedList>
      )}

      <Dialog open={!!selected} onClose={() => setSelected(null)} fullScreen={fullScreen} fullWidth maxWidth="sm">
        <Toolbar disableGutters sx={{ px: 1, pt: "env(safe-area-inset-top)" }}>
          <IconButton onClick={() => setSelected(null)} aria-label="Back to history"><ArrowBack /></IconButton>
          <Typography variant="h6" component="p" sx={{ ml: 1 }}>Payment</Typography>
        </Toolbar>
        <Stack sx={{ p: 2, pb: "calc(env(safe-area-inset-bottom) + 16px)" }}>
          {selected && (
            <PaymentStatus
              config={config}
              provider={provider}
              address={address}
              id={selected}
              viewToken={record?.viewToken}
              fallbackPayee={record ? { upiId: record.upiId, name: record.name } : undefined}
              onChanged={() => void load()}
            />
          )}
        </Stack>
      </Dialog>
    </Stack>
  );
}
