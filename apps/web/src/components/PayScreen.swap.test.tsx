import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account, AppConfig, Quote } from "../lib/api";
import PayScreen from "./PayScreen";

// The Circle packages and viem are mocked: the component test never touches the network or a wallet.
const kit = vi.hoisted(() => ({
  estimate: vi.fn(),
  swap: vi.fn(),
  createAdapter: vi.fn(),
  readContract: vi.fn(),
  constructed: vi.fn(),
}));

vi.mock("@circle-fin/swap-kit", () => ({
  SwapKit: function SwapKit() {
    kit.constructed();
    return { estimate: kit.estimate, swap: kit.swap };
  },
}));
vi.mock("@circle-fin/adapter-viem-v2", () => ({ createViemAdapterFromProvider: kit.createAdapter }));
vi.mock("viem", async (importOriginal) => ({
  ...(await importOriginal<typeof import("viem")>()),
  createPublicClient: () => ({ readContract: kit.readContract }),
}));

const apiMock = vi.hoisted(() => ({ account: vi.fn(), quote: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, account: apiMock.account, quote: apiMock.quote } };
});

const ADDRESS = "0xAbCdEf0000000000000000000000000000000001";

const CONFIG = {
  chains: {
    arc: {
      chainId: 5042002, name: "Arc Testnet", rpc: "https://rpc.arc.test", explorer: "https://explorer.arc.test",
      vault: "0x0000000000000000000000000000000000000001",
      token: { symbol: "USDC", address: "0x3600000000000000000000000000000000000000", decimals: 6 },
    },
    whitechain: {
      chainId: 1874, name: "Whitechain Sepolia", rpc: "https://rpc.wc.test", explorer: "https://explorer.wc.test",
      vault: "0x0000000000000000000000000000000000000002",
      token: { symbol: "USDT", address: "0x0000000000000000000000000000000000000003", decimals: 6 },
    },
  },
  limits: { minInrPaise: "100", maxInrPaise: "100000", dailyUserInrPaise: "500000" },
  spreadBps: 80,
  quoteTtlSec: 90,
} as unknown as AppConfig;

const quote = (amount = "2000000"): Quote => ({
  id: "q1",
  intent: { amount },
  payee: { upiId: "shop@ybl", name: "Shop", amountInQr: "10.00" },
  display: {
    amount: "2", symbol: "USDC", rate: "99.3", marketRate: "100.1", spreadBps: 80, networkFee: "₹0",
    expiresAt: String(Math.floor(Date.now() / 1000) + 90),
  },
  signing: { permit: {} as never, intent: {} as never },
  viewToken: "t",
});

const account = (balance: string, formatted: string): Account => ({ balance, balanceFormatted: formatted, symbol: "USDC" });

/** A stand-in for Circle's estimate: the pool pays 1.08 USDC per EURC at the stop limit. */
function estimateFor(params: { amountIn: string }) {
  const amountIn = Number(params.amountIn);
  return {
    tokenIn: "EURC", tokenOut: "USDC", chainIn: "Arc_Testnet", chainOut: "Arc_Testnet",
    fromAddress: ADDRESS, toAddress: ADDRESS, amountIn: params.amountIn,
    stopLimit: { token: "USDC", amount: (amountIn * 1.08).toFixed(6) },
    estimatedOutput: { token: "USDC", amount: (amountIn * 1.1).toFixed(6) },
    fees: [{ type: "gas", token: "USDC", amount: "0.002" }],
  };
}

function provider() {
  return { request: vi.fn(async ({ method }: { method: string }) => (method === "eth_chainId" ? "0x4cef52" : null)) };
}

async function openReview(chain: "arc" | "whitechain") {
  const user = userEvent.setup();
  render(<PayScreen config={CONFIG} provider={provider()} address={ADDRESS} />);
  await user.click(screen.getByRole("button", { name: "Enter a UPI link" }));
  await user.type(screen.getByLabelText("UPI link"), "upi://pay?pa=shop@ybl&pn=Shop&am=10.00");
  await user.click(screen.getByRole("button", { name: "Continue" }));
  if (chain === "whitechain") await user.click(await screen.findByRole("button", { name: /USDT/ }));
  await waitFor(() => expect(apiMock.account).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByRole("heading", { name: "Review payment" });
  return user;
}

beforeEach(() => {
  kit.estimate.mockImplementation(async (params) => estimateFor(params));
  kit.createAdapter.mockResolvedValue({ adapter: true });
  kit.readContract.mockResolvedValue(5_000_000n); // 5 EURC
  apiMock.quote.mockImplementation(async () => quote());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  apiMock.account.mockReset();
  kit.swap.mockReset();
});

describe("Swap EURC to USDC on the review screen", () => {
  it("shows the swap card on Arc when the USDC balance is too low", async () => {
    apiMock.account.mockResolvedValue(account("500000", "0.5"));
    await openReview("arc");
    const button = await screen.findByRole("button", { name: "Swap EURC → USDC" });
    expect(button).toBeEnabled();
    expect(screen.getByText(/You have/)).toHaveTextContent("You have 0.5 USDC. This payment needs 2 USDC.");
    expect(await screen.findByText("Your EURC: 5")).toBeInTheDocument();
    // Pay stays disabled while the balance is too low.
    expect(screen.getByRole("button", { name: /^Pay/ })).toBeDisabled();
    expect(screen.queryByText(/doesn't have enough/)).not.toBeInTheDocument();
    // Nothing was loaded or sent until the user pressed the button.
    expect(kit.constructed).not.toHaveBeenCalled();
    expect(kit.swap).not.toHaveBeenCalled();
  });

  it("does not show it on Arc when the balance is enough", async () => {
    apiMock.account.mockResolvedValue(account("5000000", "5"));
    await openReview("arc");
    expect(screen.queryByRole("button", { name: "Swap EURC → USDC" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Pay/ })).toBeEnabled();
  });

  it("does not show it on Whitechain, which keeps today's warning", async () => {
    apiMock.account.mockResolvedValue(account("500000", "0.5"));
    await openReview("whitechain");
    expect(screen.queryByRole("button", { name: "Swap EURC → USDC" })).not.toBeInTheDocument();
    expect(screen.getByText(/doesn't have enough/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Pay/ })).toBeDisabled();
  });

  it("sizes the swap, asks for confirmation, swaps, then refreshes the balance and the quote", async () => {
    apiMock.account
      .mockResolvedValueOnce(account("500000", "0.5")) // details screen
      .mockResolvedValueOnce(account("2100000", "2.1")); // after the swap
    kit.swap.mockResolvedValue({
      txHash: "0xabc", explorerUrl: "https://explorer.arc.test/tx/0xabc", amountOut: "1.6",
      progress: { status: "DONE", substatus: "COMPLETED" },
    });
    const user = await openReview("arc");
    expect(apiMock.quote).toHaveBeenCalledTimes(1);

    await user.click(await screen.findByRole("button", { name: "Swap EURC → USDC" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm the swap" });
    // Shortfall 1.5 USDC, target 1.595, rate 1.08 at the stop limit: sell 1.476852 EURC, get at least 1.595 USDC.
    expect(dialog).toHaveTextContent("Swap 1.476852 EURC → at least 1.595 USDC");
    expect(dialog).toHaveTextContent("Network fee: 0.002 USDC");
    expect(dialog).toHaveTextContent("Rates come from Circle. You pay the network fee for this step.");
    // Estimates only so far: no swap without the user's click.
    expect(kit.swap).not.toHaveBeenCalled();
    expect(kit.estimate).toHaveBeenCalledWith(expect.objectContaining({
      tokenIn: "EURC", tokenOut: "USDC", from: expect.objectContaining({ chain: "Arc_Testnet" }),
    }));

    await user.click(within(dialog).getByRole("button", { name: "Swap" }));
    await waitFor(() => expect(kit.swap).toHaveBeenCalledTimes(1));
    expect(kit.swap).toHaveBeenCalledWith(expect.objectContaining({ amountIn: "1.476852", tokenIn: "EURC", tokenOut: "USDC" }));

    // Fresh balance, fresh quote, and the card is gone because the balance now covers the payment.
    await waitFor(() => expect(apiMock.account).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(apiMock.quote).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Swap EURC → USDC" })).not.toBeInTheDocument());
    const link = await screen.findByRole("link", { name: "View the swap transaction" });
    expect(link).toHaveAttribute("href", "https://explorer.arc.test/tx/0xabc");
    await waitFor(() => expect(screen.getByRole("button", { name: /^Pay/ })).toBeEnabled());
  });

  it("stops before sending anything when the estimate is for another wallet", async () => {
    apiMock.account.mockResolvedValue(account("500000", "0.5"));
    kit.estimate.mockImplementation(async (params) => ({
      ...estimateFor(params), toAddress: "0x0000000000000000000000000000000000000bad",
    }));
    const user = await openReview("arc");
    await user.click(await screen.findByRole("button", { name: "Swap EURC → USDC" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("different wallet address");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(kit.swap).not.toHaveBeenCalled();
  });

  it("says so when the EURC balance can't cover the payment", async () => {
    apiMock.account.mockResolvedValue(account("500000", "0.5"));
    kit.readContract.mockResolvedValue(1_000_000n); // 1 EURC is worth about 1.08 USDC, short of 1.5
    const user = await openReview("arc");
    await user.click(await screen.findByRole("button", { name: "Swap EURC → USDC" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Not enough EURC to cover this payment either.");
    expect(kit.swap).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Swap EURC → USDC" })).toBeEnabled();
  });

  it("explains a cancelled wallet prompt and doesn't stay busy", async () => {
    apiMock.account.mockResolvedValue(account("500000", "0.5"));
    kit.swap.mockRejectedValue(Object.assign(new Error("User rejected the request."), { code: 4001 }));
    const user = await openReview("arc");
    await user.click(await screen.findByRole("button", { name: "Swap EURC → USDC" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm the swap" });
    await user.click(within(dialog).getByRole("button", { name: "Swap" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You cancelled this in your wallet.");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Swap EURC → USDC" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Back" })).toBeEnabled();
    expect(apiMock.quote).toHaveBeenCalledTimes(1);
  });

  it("explains a liquidity failure after a good estimate", async () => {
    apiMock.account.mockResolvedValue(account("500000", "0.5"));
    kit.swap.mockRejectedValue(new Error("ONCHAIN_SIMULATION_FAILED: execution reverted"));
    const user = await openReview("arc");
    await user.click(await screen.findByRole("button", { name: "Swap EURC → USDC" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm the swap" });
    await user.click(within(dialog).getByRole("button", { name: "Swap" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The swap pool is short of liquidity right now.");
  });

  it("explains a missing gas balance", async () => {
    apiMock.account.mockResolvedValue(account("0", "0"));
    kit.swap.mockRejectedValue(new Error("insufficient funds for gas * price + value"));
    const user = await openReview("arc");
    await user.click(await screen.findByRole("button", { name: "Swap EURC → USDC" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm the swap" });
    await user.click(within(dialog).getByRole("button", { name: "Swap" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You need a little USDC on Arc to pay the network fee for the swap.");
  });
});
