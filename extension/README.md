# RupeeRail Agents extension

Your own long-lived agents. Choose one, type a prompt, and it browses in your Chrome and pays the UPI QR at checkout from its own wallet, without asking, up to its limit. The Stop button works until the payment is submitted.

The extension holds no keys. It reads the page and carries out actions, while `agent-server/` decides and signs. It stays on the tab it started in, and refuses to read or act on any site outside that agent's allowed sites. It never reads or types into password or one-time-code fields.

## Try it

1. **Start the agent server.** It only listens on this machine, and only the extension can use it from a browser. Agents are kept in `agent-server/data/agents.sqlite`:

   ```bash
   cd agent-server
   uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8790
   ```

2. **Start the mock stores,** if you have them. They're kept locally and aren't in git. `/` is a simple shop, `/foodie/` a food-delivery site and `/bazaar/` a marketplace:

   ```bash
   python3 -m http.server 5180 --bind 127.0.0.1 --directory apps/mock-store
   ```

3. **Build and load the extension:**

   ```bash
   cd extension
   npm install
   npm run build
   ```

   In Chrome, open `chrome://extensions`, turn on Developer mode, choose **Load unpacked**, and select `extension/dist`. Reload it there after each build.

   To skip building, download the zip from the web app's Agents Hub and load the unzipped folder instead. Chrome derives an unpacked extension's ID from its folder, so if you set `AGENT_EXTENSION_ID`, set it to the ID Chrome shows for that folder.

4. **Run an agent.** Click the extension's icon to open the side panel, choose an agent or make one, type a prompt, and set the limit to ₹1.

**This pays for real if the phone payer is running live.** The agent's Circle wallet locks test USDC, and the phone sends real rupees to the QR's payee. Unlock the phone first, or the payment fails and the panel says **Not paid**.

Without `OPENAI_API_KEY`, the agent uses a rule-based decider, which isn't AI and only suits simple stores. Each step in the panel shows what decided it.

## RupeeRail's hosted server

A person with no agent server of their own sees **Use RupeeRail's server** in the panel. Its address is fixed in `src/connection.ts`, and **Stop using it** moves back. See `agent-server/README.md`.

It's an open demo: anyone can read, change and run its agents. It also runs one agent at a time, because there's one phone, so anyone else gets "an agent is already running".

## Making an agent

Each new agent takes one of Circle's 5 wallets per chain, unless a wallet is unassigned, and archiving doesn't free one.

Its instructions and "Things it should know" are sent to OpenAI on every step, along with page text, including any name or address shown. They stay on this computer otherwise.

Tick **Rehearse** to try a new site. The agent browses as usual, stops before ordering or paying, and reports what a real run would need, including any off-site hosts such as a payment gateway.

## Real sites

Log in to the site in Chrome first, and add it to the agent's allowed sites. The agent doesn't log in, or handle OTPs or CAPTCHAs: it pauses, and you click **I've handled it, continue**.

When the agent wants to click something like "Place order", it goes ahead on its own only if UPI is visibly selected and the total is within the limit. If it can't confirm UPI, it pauses and asks you, because that click could spend through a saved card or cash on delivery, outside all of the agent's limits. Over the limit is refused.

## Real clicks

Many sites ignore scripted clicks, so clicks go through Chrome's debugger access as real input. It's attached only to the run's tab, after the site check, and only during a run. Chrome shows a banner saying the extension is debugging the browser. If you click **Cancel** on the banner, the run carries on with scripted clicks, which some sites ignore. A click on something covered by a pop-up is refused, so the agent closes the pop-up first.

## Results

| Panel says | Meaning |
|---|---|
| Paid | The phone payer recorded the merchant as paid |
| Not paid | The payer failed, or the funds were returned. Dispute it if they weren't returned |
| Check the phone | Pay was tapped but the result wasn't clear |
| Dry run | The payer was in dry-run mode, so no rupees moved |
| No result | The payer didn't report within 2 minutes, and may be offline |
| Refused | A safety check stopped it before signing |
| Rehearsed | It stopped before ordering or paying, as asked |

## Test

```bash
npx playwright install chromium
npm run build
npx playwright test
```

The tests run the real extension, agent server and mock stores in Chromium against a fake off-ramp, with a throwaway local key for each agent, so they use no testnet, wallet or phone. They skip without the local mock stores. The stack also serves an invite-only server on `:8794`, for the hosted server test.

Set `E2E_OPENAI=1` to include the tests that use the real model. They use up your OpenAI requests, and `OPENAI_MODEL` chooses the model.
