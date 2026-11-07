import AccountBalanceWalletOutlined from "@mui/icons-material/AccountBalanceWalletOutlined";
import AddCardOutlined from "@mui/icons-material/AddCardOutlined";
import InstallMobileOutlined from "@mui/icons-material/InstallMobileOutlined";
import QrCodeScanner from "@mui/icons-material/QrCodeScanner";
import ReceiptLongOutlined from "@mui/icons-material/ReceiptLongOutlined";
import SmartToyOutlined from "@mui/icons-material/SmartToyOutlined";
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
import { useCallback, useEffect, useRef, useState } from "react";
import AgentsScreen from "./components/AgentsScreen";
import BuyScreen from "./components/BuyScreen";
import ConnectScreen from "./components/ConnectScreen";
import HistoryScreen from "./components/HistoryScreen";
import OnrampStatus from "./components/OnrampStatus";
import PayScreen from "./components/PayScreen";
import SideNav from "./components/SideNav";
import { api, type AppConfig } from "./lib/api";
import { parseAgentsPath } from "./lib/arkiv";
import { shortAddress } from "./lib/format";
import { isUnverifiedReturn, parseOnrampReturn, stripOnrampReturn } from "./lib/onramp";
import {
  connectWallet, discoverWallets, existingAccount, walletErrorMessage, type Eip1193Provider, type WalletOption,
} from "./lib/wallet";

const WALLET_KEY = "rupeerail.wallet";

type Tab = "pay" | "buy" | "history" | "agents";
const PAY_TAB = { value: "pay" as const, label: "Pay", icon: <QrCodeScanner /> };
const BUY_TAB = { value: "buy" as const, label: "Buy", icon: <AddCardOutlined /> };
const HISTORY_TAB = { value: "history" as const, label: "History", icon: <ReceiptLongOutlined /> };
const AGENTS_TAB = { value: "agents" as const, label: "Agents", icon: <SmartToyOutlined /> };

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
  const [returned, setReturned] = useState(() => parseOnrampReturn(window.location.search));
  const [unverifiedReturn, setUnverifiedReturn] = useState(() => isUnverifiedReturn(window.location.search));
  // undefined when the address isn't /agents, null for the directory, and otherwise one agent's wallet.
  const [agentsPath, setAgentsPath] = useState(() => parseAgentsPath(window.location.pathname));
  const [tab, setTab] = useState<Tab>(() => (returned ? "buy" : agentsPath !== undefined ? "agents" : "pay"));
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [installPrompt, setInstallPrompt] = useState<InstallPromptEvent | null>(null);
  const restored = useRef(false);
  // Material 3's "expanded" width: from here the bottom bar becomes a sidebar.
  const wide = useMediaQuery("(min-width:840px)", { noSsr: true });
  const sidebar = wide && !!address;

  // Buy is offered only when the server collects through Razorpay. The manual rail needs an operator and a real UPI ID.
  const tabs = config?.onramp?.rail === "razorpay" ? [PAY_TAB, BUY_TAB, HISTORY_TAB, AGENTS_TAB] : [PAY_TAB, HISTORY_TAB, AGENTS_TAB];

  const go = useCallback((path: string) => {
    if (window.location.pathname !== path) window.history.pushState(null, "", path);
    setAgentsPath(parseAgentsPath(path));
  }, []);

  const chooseTab = (value: Tab) => {
    setReturned(null);  // the Razorpay return is shown once; coming back to Buy starts fresh
    setTab(value);
    go(value === "agents" ? "/agents" : "/");
  };

  useEffect(() => {
    const onPop = () => {
      const path = parseAgentsPath(window.location.pathname);
      setAgentsPath(path);
      setTab((current) => (path !== undefined ? "agents" : current === "agents" ? "pay" : current));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const loadConfig = useCallback(() => {
    setConfigError(null);
    api.config().then(setConfig).catch((e: Error) => setConfigError(e.message));
  }, []);
  useEffect(loadConfig, [loadConfig]);
  useEffect(() => {
    // Read once, then drop it from the address bar so a reload doesn't show the return again.
    if (parseOnrampReturn(window.location.search) || isUnverifiedReturn(window.location.search)) {
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
    go("/");
  }, [go]);

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
      {sidebar && <SideNav tabs={tabs} value={tab} onChange={chooseTab} />}
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
          {unverifiedReturn && (
            <Alert severity="warning" onClose={() => setUnverifiedReturn(false)} sx={{ mb: 2 }}>
              We couldn't check that return from Razorpay. If you paid, we still confirm it with Razorpay directly, and your tokens are sent once it's confirmed.
            </Alert>
          )}
          {tab === "agents" ? (
            <AgentsScreen
              wallet={agentsPath ?? null}
              onOpen={(wallet) => go(wallet ? `/agents/${wallet}` : "/agents")}
              onExit={address ? undefined : () => chooseTab("pay")}
            />
          ) : configError ? (
            <Alert severity="error" action={<Button color="inherit" size="small" onClick={loadConfig}>Retry</Button>}>
              {configError}
            </Alert>
          ) : !config || wallets === null ? (
            <Box sx={{ display: "grid", placeItems: "center", py: 10 }}>
              <CircularProgress aria-label="Loading" />
            </Box>
          ) : !address || !provider ? (
            <Stack spacing={3}>
              {returned && <OnrampStatus config={config} orderId={returned.orderId} justPaid={returned.payment === "received"} />}
              <ConnectScreen wallets={wallets} connecting={connecting} error={walletError} onConnect={connect} />
              <Button variant="text" startIcon={<SmartToyOutlined />} onClick={() => chooseTab("agents")} sx={{ alignSelf: "center" }}>
                Explore the Agents Hub
              </Button>
            </Stack>
          ) : tab === "pay" ? (
            <PayScreen key={address} config={config} provider={provider} address={address} onOpenAgents={() => chooseTab("agents")} />
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
            {tabs.map(({ value, label, icon }) => <BottomNavigationAction key={value} label={label} value={value} icon={icon} />)}
          </BottomNavigation>
        </Paper>
      )}
    </Box>
  );
}
