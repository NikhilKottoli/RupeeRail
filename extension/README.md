# RupeeRail Agent extension

Tell an agent what to buy. It shops in your current tab and pays the UPI QR at checkout from its wallet, without asking, up to your limit. The Stop button works until the payment is submitted.

The extension holds no keys. It reads the page and carries out actions, while `agent-server/` decides and signs. It stays on the tab it started in, and refuses to read or act on any site outside the server's allowed list. It never reads or types into password or one-time-code fields.

## Try it

1. **Start the agent server.** It only listens on this machine, and only the extension can use it from a browser:

   ```bash
   cd agent-server
   uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8790
   ```

2. **Start the mock store:**

   ```bash
   python3 -m http.server 5180 --bind 127.0.0.1 --directory apps/mock-store
   ```

3. **Build and load the extension:**

   ```bash
   cd extension
   npm install
   npm run build
   ```

   In Chrome, open `chrome://extensions`, turn on Developer mode, choose **Load unpacked**, and select `extension/dist`.

4. **Run a goal.** Open `http://127.0.0.1:5180`, click the extension's icon to open the side panel, type "Order a cutting chai", and set the limit to ₹1.

**This pays for real if the phone payer is running live.** The Circle wallet locks test USDC, and the phone sends real rupees to the QR's payee.

Without `OPENAI_API_KEY`, the agent uses a rule-based decider, which isn't AI and only suits simple stores. Each step in the panel shows what decided it.

## Real sites

Add the site to `AGENT_ALLOWED_DOMAINS`. Page text from every step, including any name or address shown, goes to OpenAI.

When the agent wants to click something like "Place order" or "Buy now", it pauses. Check that UPI is selected, not a saved card or cash on delivery, then click continue. These clicks could otherwise spend outside all of the agent's limits.

## Results

| Panel says | Meaning |
|---|---|
| Paid | The phone payer recorded the merchant as paid |
| Not paid | The payer failed, or the funds were returned. Dispute it if they weren't returned |
| Check the phone | Pay was tapped but the result wasn't clear |
| Dry run | The payer was in dry-run mode, so no rupees moved |
| No result | The payer didn't report within 2 minutes, and may be offline |
| Refused | A safety check stopped it before signing |

## Test

```bash
npx playwright install chromium
npm run build
npx playwright test
```

The tests run the real extension, agent server and mock store in Chromium against a fake off-ramp, so they use no testnet, wallet or phone.
