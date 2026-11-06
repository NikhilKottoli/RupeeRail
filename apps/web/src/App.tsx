import AccountBalanceWalletOutlined from "@mui/icons-material/AccountBalanceWalletOutlined";
import AddCardOutlined from "@mui/icons-material/AddCardOutlined";
import InstallMobileOutlined from "@mui/icons-material/InstallMobileOutlined";
import QrCodeScanner from "@mui/icons-material/QrCodeScanner";
import ReceiptLongOutlined from "@mui/icons-material/ReceiptLongOutlined";
import Alert from "@mui/material/Alert";
import AppBar from "@mui/material/AppBar";
import BottomNavigation from "@mui/material/BottomNavigation";
import BottomNavigationAction from "@mui/material/BottomNavigationAction";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Container from "@mui/material/Container";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Toolbar from "@mui/material/Toolbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import Stack from "@mui/material/Stack";
import useMediaQuery from "@mui/material/useMediaQuery";
import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import BuyScreen from "./components/BuyScreen";
import ConnectScreen from "./components/ConnectScreen";
import HistoryScreen from "./components/HistoryScreen";
import OnrampStatus from "./components/OnrampStatus";
import PayScreen from "./components/PayScreen";
import SideNav from "./components/SideNav";
import { api, type AppConfig } from "./lib/api";
import { shortAddress } from "./lib/format";
import { parseOnrampReturn, stripOnrampReturn } from "./lib/onramp";
import {
  connectWallet, discoverWallets, existingAccount, walletErrorMessage, type Eip1193Provider, type WalletOption,
} from "./lib/wallet";

const WALLET_KEY = "rupeerail.wallet";
// Set VITE_BUY_TAB=off when building for a server that isn't ready to take Razorpay payments.
const BUY_ENABLED = import.meta.env.VITE_BUY_TAB !== "off";

type Tab = "pay" | "buy" | "history";
const TABS: { value: Tab; label: string; icon: JSX.Element }[] = [
  { value: "pay", label: "Pay", icon: <QrCodeScanner /> },
  ...(BUY_ENABLED ? [{ value: "buy" as const, label: "Buy", icon: <AddCardOutlined /> }] : []),
  { value: "history", label: "History", icon: <ReceiptLongOutlined /> },
];

interface InstallPromptEvent extends Event {
  prompt(): Promise<void>;
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [wallets, setWallets] = useState<WalletOption[] | null>(null);
  const [provider, setProvider] = useState<Eip1193Provider | null>(null);
  const [address, setAddress] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [walletError, setWalletError] = useState<string | null>(null);
  // Razorpay sends the browser back to /?onramp=<orderId>&payment=received after a purchase.
  const [returned, setReturned] = useState(() => (BUY_ENABLED ? parseOnrampReturn(window.location.search) : null));
  const [tab, setTab] = useState<Tab>(() => (returned ? "buy" : "pay"));
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [installPrompt, setInstallPrompt] = useState<InstallPromptEvent | null>(null);
  const restored = useRef(false);
  // Material 3's "expanded" width: from here the bottom bar becomes a sidebar.
  const wide = useMediaQuery("(min-width:840px)", { noSsr: true });
  const sidebar = wide && !!address;

  const chooseTab = (value: Tab) => {
    setReturned(null);  // the Razorpay return is shown once; coming back to Buy starts fresh
    setTab(value);
  };

  const loadConfig = useCallback(() => {
    setConfigError(null);
    api.config().then(setConfig).catch((e: Error) => setConfigError(e.message));
  }, []);
  useEffect(loadConfig, [loadConfig]);
  useEffect(() => {
    // Read once, then drop it from the address bar so a reload doesn't show the return again.
    if (parseOnrampReturn(window.location.search)) {
      window.history.replaceState(window.history.state, "", stripOnrampReturn(window.location.href));
    }
  }, []);
  useEffect(() => discoverWallets(setWallets), []);

  useEffect(() => {
    const onPrompt = (event: Event) => {
      event.preventDefault();
      setInstallPrompt(event as InstallPromptEvent);
    };
    const onInstalled = () => setInstallPrompt(null);
    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  // Reconnect silently to the wallet used last time, without a prompt.
  useEffect(() => {
    if (restored.current || !wallets?.length) return;
    const saved = localStorage.getItem(WALLET_KEY);
    const wallet = wallets.find((w) => w.id === saved);
    if (!wallet) return;
    restored.current = true;
    void existingAccount(wallet.provider).then((account) => {
      if (account) {
        setProvider(wallet.provider);
        setAddress(account);
      }
    });
  }, [wallets]);

  const disconnect = useCallback(() => {
    localStorage.removeItem(WALLET_KEY);
    setProvider(null);
    setAddress(null);
    setTab("pay");
  }, []);

  useEffect(() => {
    if (!provider?.on) return;
    const onAccounts = (accounts: string[]) => {
      if (accounts[0]) setAddress(accounts[0]);
      else disconnect();
    };
    provider.on("accountsChanged", onAccounts);
    return () => provider.removeListener?.("accountsChanged", onAccounts);
  }, [provider, disconnect]);

  const connect = async (wallet: WalletOption) => {
    setConnecting(true);
    setWalletError(null);
    try {
      const account = await connectWallet(wallet.provider);
      localStorage.setItem(WALLET_KEY, wallet.id);
      setProvider(wallet.provider);
      setAddress(account);
    } catch (e) {
      setWalletError(walletErrorMessage(e));
    } finally {
      setConnecting(false);
    }
  };

  const install = async () => {
    await installPrompt?.prompt();
    setInstallPrompt(null);
  };

  return (
    <Box sx={{ minHeight: "100dvh", display: "flex" }}>
      {sidebar && <SideNav tabs={TABS} value={tab} onChange={chooseTab} />}
      <Box sx={{ flexGrow: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <AppBar position="sticky" elevation={0} color="transparent" sx={{ bgcolor: "background.default", pt: "env(safe-area-inset-top)" }}>
          <Toolbar sx={{ gap: 1 }}>
            {sidebar
              ? <Box sx={{ flexGrow: 1 }} />
              : <Typography variant="h6" component="h1" sx={{ flexGrow: 1 }}>RupeeRail</Typography>}
            {installPrompt && (
              <Tooltip title="Install app">
                <IconButton onClick={install} aria-label="Install app"><InstallMobileOutlined /></IconButton>
              </Tooltip>
            )}
            {address && (
              <Chip
                icon={<AccountBalanceWalletOutlined />}
                label={shortAddress(address)}
                variant="outlined"
                onClick={(e) => setMenuAnchor(e.currentTarget)}
                aria-label="Wallet menu"
              />
            )}
          </Toolbar>
        </AppBar>
        <Menu anchorEl={menuAnchor} open={!!menuAnchor} onClose={() => setMenuAnchor(null)}>
          <MenuItem
            onClick={() => {
              setMenuAnchor(null);
              disconnect();
            }}
          >
            Disconnect
          </MenuItem>
        </Menu>

        <Container maxWidth="sm" component="main" sx={{ flexGrow: 1, pt: 1, pb: address && !sidebar ? 12 : 4 }}>
          {configError ? (
            <Alert severity="error" action={<Button color="inherit" size="small" onClick={loadConfig}>Retry</Button>}>
              {configError}
            </Alert>
          ) : !config || wallets === null ? (
            <Box sx={{ display: "grid", placeItems: "center", py: 10 }}>
              <CircularProgress aria-label="Loading" />
            </Box>
          ) : !address || !provider ? (
            returned ? (
              <Stack spacing={3}>
                <OnrampStatus config={config} orderId={returned.orderId} justPaid={returned.payment === "received"} />
                <ConnectScreen wallets={wallets} connecting={connecting} error={walletError} onConnect={connect} />
              </Stack>
            ) : (
              <ConnectScreen wallets={wallets} connecting={connecting} error={walletError} onConnect={connect} />
            )
          ) : tab === "pay" ? (
            <PayScreen key={address} config={config} provider={provider} address={address} />
          ) : tab === "buy" ? (
            <BuyScreen key={address} config={config} address={address} returned={returned} />
          ) : (
            <HistoryScreen key={address} config={config} provider={provider} address={address} />
          )}
        </Container>

      </Box>

      {address && !sidebar && (
        <Paper
          square
          elevation={0}
          sx={{ position: "fixed", bottom: 0, left: 0, right: 0, bgcolor: "background.surface", pb: "env(safe-area-inset-bottom)", zIndex: "appBar" }}
        >
          <BottomNavigation showLabels value={tab} onChange={(_, value) => chooseTab(value)}>
            {TABS.map(({ value, label, icon }) => <BottomNavigationAction key={value} label={label} value={value} icon={icon} />)}
          </BottomNavigation>
        </Paper>
      )}
    </Box>
  );
}
