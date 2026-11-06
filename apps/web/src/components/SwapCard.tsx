import SwapHoriz from "@mui/icons-material/SwapHoriz";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { SwapEstimate, SwapResult } from "@circle-fin/swap-kit";
import { useEffect, useRef, useState } from "react";
import type { ChainInfo } from "../lib/api";
import {
  assertEstimateSafe, createArcSwapper, gasFeeText, minOutOf, readEurcBalance, safeExplorerUrl, sizeSwap,
  swapErrorMessage, unitsToDecimal, type ArcSwapper,
} from "../lib/swap";
import { ensureChain, type Eip1193Provider } from "../lib/wallet";

interface Plan {
  amountIn: bigint;
  minOut: bigint;
  estimate: SwapEstimate;
}

interface Props {
  chainKey: string;
  chain: ChainInfo;
  provider: Eip1193Provider;
  address: string;
  /** What the wallet holds and what the payment needs, both in USDC base units. */
  balance: bigint;
  required: bigint;
  /** The same two amounts, as shown to the user. */
  balanceText: string;
  requiredText: string;
  symbol: string;
  disabled: boolean;
  /** Called with true while the card is working, so the screen can lock the Pay button. */
  onBusyChange: (busy: boolean) => void;
  /** The swap went through. The screen refreshes the balance and the quote, then this resolves. */
  onSwapped: (result: SwapResult) => Promise<void>;
}

export function SwapSuccess({ result, chain }: { result: SwapResult; chain: ChainInfo }) {
  const url = safeExplorerUrl(result, chain.explorer);
  return (
    <Alert severity="success" role="status">
      Swap complete{result.amountOut ? `: you received ${result.amountOut} USDC` : ""}.{" "}
      {url && <Link href={url} target="_blank" rel="noopener noreferrer">View the swap transaction</Link>}
    </Alert>
  );
}

export default function SwapCard({
  chainKey, chain, provider, address, balance, required, balanceText, requiredText, symbol, disabled, onBusyChange, onSwapped,
}: Props) {
  const [eurc, setEurc] = useState<bigint | null | "error">(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const swapper = useRef<ArcSwapper | null>(null);

  useEffect(() => {
    let current = true;
    readEurcBalance(chain.rpc, address)
      .then((value) => current && setEurc(value))
      .catch(() => current && setEurc("error"));
    return () => {
      current = false;
    };
  }, [chain.rpc, address]);

  const working = busy !== null;
  const setWorking = (message: string | null) => {
    setBusy(message);
    onBusyChange(message !== null);
  };

  /** Steps 1 to 4: switch network, read EURC, size the swap with Circle's estimates, then ask for confirmation. */
  const prepare = async () => {
    setError(null);
    setWorking("Checking the swap rate…");
    try {
      await ensureChain(provider, chainKey, chain);
      const eurcBalance = await readEurcBalance(chain.rpc, address);
      setEurc(eurcBalance);
      const kit = await createArcSwapper(provider, {
        onPrompt: () => setWorking("Confirm the swap in your wallet…"),
        onSent: () => setWorking("Swapping…"),
      });
      const sized = await sizeSwap({
        shortfall: required - balance,
        eurcBalance,
        estimate: async (amountIn) => {
          const estimate = await kit.estimate(amountIn);
          assertEstimateSafe(estimate, address);
          return { estimate, minOut: minOutOf(estimate) };
        },
      });
      swapper.current = kit;
      setPlan({ amountIn: sized.amountIn, minOut: sized.quote.minOut, estimate: sized.quote.estimate });
    } catch (e) {
      setError(swapErrorMessage(e));
    } finally {
      setWorking(null);
    }
  };

  /** Steps 5 to 8. Only ever called from the Swap button in the dialog, which is the user's confirmation. */
  const confirm = async () => {
    const reviewed = plan;
    const kit = swapper.current;
    if (!reviewed || !kit) return;
    setError(null);
    setWorking("Confirm the swap in your wallet…");
    try {
      assertEstimateSafe(reviewed.estimate, address);
      const result = await kit.swap(reviewed.amountIn);
      if (result.progress?.status === "FAILED" || result.progress?.status === "NOT_FOUND") {
        throw new Error("The swap didn't go through. Nothing was taken from your wallet except the network fee.");
      }
      setPlan(null);
      setWorking("Updating your balance…");
      await onSwapped(result);
    } catch (e) {
      setPlan(null);
      setError(swapErrorMessage(e));
    } finally {
      setWorking(null);
    }
  };

  const eurcText = eurc === null ? "Checking your EURC…"
    : eurc === "error" ? "We couldn't read your EURC balance."
      : `Your EURC: ${unitsToDecimal(eurc)}`;
  const noEurc = typeof eurc === "bigint" && eurc === 0n;
  const fee = plan ? gasFeeText(plan.estimate) : null;

  return (
    <Card variant="outlined">
      <CardContent>
        <Stack spacing={1.5}>
          <Typography>
            You have <strong>{balanceText} {symbol}</strong>. This payment needs <strong>{requiredText} {symbol}</strong>.
          </Typography>
          <Typography variant="body2" color="text.secondary">{eurcText}</Typography>
          {error && <Alert severity="error" role="alert">{error}</Alert>}
          {working && !plan && (
            <Stack direction="row" spacing={1.5} role="status" sx={{ alignItems: "center" }}>
              <CircularProgress size={20} />
              <Typography variant="body2">{busy}</Typography>
            </Stack>
          )}
          <Button
            variant="contained"
            size="large"
            startIcon={<SwapHoriz />}
            disabled={disabled || working || noEurc}
            onClick={prepare}
          >
            Swap EURC → USDC
          </Button>
          {noEurc && <Typography variant="caption" color="text.secondary">You don't have any EURC to swap.</Typography>}
        </Stack>
      </CardContent>

      <Dialog open={plan !== null} onClose={() => !working && setPlan(null)} aria-labelledby="swap-title" fullWidth maxWidth="xs">
        <DialogTitle id="swap-title">Confirm the swap</DialogTitle>
        <DialogContent>
          {plan && (
            <Stack spacing={1.5}>
              <Typography>
                Swap <strong>{unitsToDecimal(plan.amountIn)} EURC</strong> → at least <strong>{unitsToDecimal(plan.minOut)} USDC</strong>
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Network fee: {fee ?? "shown by your wallet"}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Rates come from Circle. You pay the network fee for this step. The swap is routed through a swap
                provider and is subject to its terms.
              </Typography>
              {working && (
                <Stack direction="row" spacing={1.5} role="status" sx={{ alignItems: "center" }}>
                  <CircularProgress size={20} />
                  <Typography variant="body2">{busy}</Typography>
                </Stack>
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button disabled={working} onClick={() => setPlan(null)}>Cancel</Button>
          <Button variant="contained" disabled={working} onClick={confirm}>Swap</Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
