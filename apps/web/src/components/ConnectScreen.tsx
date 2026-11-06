import AccountBalanceWalletOutlined from "@mui/icons-material/AccountBalanceWalletOutlined";
import Alert from "@mui/material/Alert";
import Avatar from "@mui/material/Avatar";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { isMobileDevice, metaMaskDeepLink, type WalletOption } from "../lib/wallet";

interface Props {
  wallets: WalletOption[];
  connecting: boolean;
  error: string | null;
  onConnect: (wallet: WalletOption) => void;
}

export default function ConnectScreen({ wallets, connecting, error, onConnect }: Props) {
  return (
    <Stack spacing={3} sx={{ pt: 4 }}>
      <Stack spacing={1}>
        <Typography variant="h4" component="h2">Pay any UPI QR with stablecoins</Typography>
        <Typography color="text.secondary">
          Scan, sign in your wallet, and the merchant gets rupees. You don't pay any gas.
        </Typography>
      </Stack>

      {error && <Alert severity="error">{error}</Alert>}

      {wallets.length > 0 ? (
        <Stack spacing={1.5}>
          {wallets.map((wallet) => (
            <Button
              key={wallet.id}
              variant="contained"
              size="large"
              disabled={connecting}
              onClick={() => onConnect(wallet)}
              startIcon={wallet.icon
                ? <Avatar src={wallet.icon} alt="" variant="rounded" sx={{ width: 24, height: 24 }} />
                : <AccountBalanceWalletOutlined />}
            >
              {connecting ? "Check your wallet…" : `Connect ${wallet.name}`}
            </Button>
          ))}
        </Stack>
      ) : isMobileDevice() ? (
        <Stack spacing={1.5}>
          <Button variant="contained" size="large" href={metaMaskDeepLink(new URL(window.location.href))}>
            Open in MetaMask
          </Button>
          <Typography variant="body2" color="text.secondary">
            RupeeRail runs inside your wallet's browser on phones. If you use another wallet, open this page in its browser.
          </Typography>
        </Stack>
      ) : (
        <Alert severity="info">
          No wallet found. Install a browser wallet such as MetaMask, then reload this page.
        </Alert>
      )}
    </Stack>
  );
}
