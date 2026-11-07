# RupeeRail agent server

Decides and signs for the extension's agents. On your own computer, follow `extension/README.md`. This page covers hosting it as an open demo, with `AGENT_OPEN_ACCESS=1`.

## What an open server means

Anyone who installs the extension from the Agents Hub can use it, and so can any script, with no sign-in:

- **Anyone can read and change every agent,** including instructions and profiles, and can start runs, change budgets and archive agents.
- **Anyone can make your phone pay.** If the payer has `UPI_LITE_LIVE=1`, a stranger's run can pay any payee, up to the payer's limits, which default to Rs 1000 per rolling 24 hours. Keep the payer in dry run or on `UPI_LITE_ALLOWLIST` while this is up.
- **Anyone can use up your accounts:** Circle's 5 wallets per chain, the testnet funds in agent wallets, and your OpenAI key.
- **It's one run at a time,** because there's one phone.

Stop the service when the demo ends. To limit it to people you choose, set `AGENT_ACCESS_CODES` instead. The server enforces codes, but the extension no longer has a field for them.

## Deploy

These steps haven't been run on the VM yet.

1. **Make the settings:**

   ```bash
   # /etc/rupeerail/agents.env, owned by root and readable by the rupeerail group only
   AGENT_OPEN_ACCESS=1
   PAYER_RESULTS_TOKEN=<a long random secret>
   OPENAI_API_KEY=<key>
   ```

2. **Copy the code to `/opt/rupeerail/agent-server`,** without `data/`, `.env` or `.venv`, and make its environment with `uv sync --frozen` on the VM.

3. **Install and log in to the Circle CLI** as the `rupeerail` user, with `HOME=/var/lib/rupeerail-agents`. Moving wallets from another machine isn't covered here: new agents get new wallets, and Circle allows 5 per chain.

4. **Install the service and Caddy's route:**

   ```bash
   sudo cp deploy/rupeerail-agents.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now rupeerail-agents
   ```

   Then deploy the web app, whose steps in `apps/web/README.md` reload `deploy/Caddyfile` and ship the extension's zip.

5. **On the machine with the phone,** set `AGENT_RESULTS_URL=https://<host>/agent-api/payer-results` and `AGENT_RESULTS_TOKEN` to the same `PAYER_RESULTS_TOKEN`, and run `uv run python main.py report` beside the payer. Without it, every real run ends in **No result**.

## Public track records

The VM must not hold the Arkiv publisher's key, so with `ARKIV_PUBLISHER_URL` unset, hosted agents publish nothing and don't appear in the Hub's directory. To publish, keep the publisher on your own machine and tunnel to it, with `ssh -N -R 8793:127.0.0.1:8793 <host>`. Then set `ARKIV_PUBLISHER_URL=http://127.0.0.1:8793` and `ARKIV_PUBLISHER_TOKEN` on the VM. Requests wait in the outbox while the tunnel is down.

## Guards that remain

- With neither `AGENT_OPEN_ACCESS=1` nor codes set, the server refuses anything carrying `X-Forwarded-For`, which Caddy always adds.
- Websites stay blocked by the `Origin` check.
- `PAYER_RESULTS_TOKEN` is the only thing that can report a payment's result.
- Limits still apply per order (`AGENT_GLOBAL_MAX_INR`) and per agent (`AGENT_MAX_BUDGET_USDC`), but nothing limits how many runs someone starts.
