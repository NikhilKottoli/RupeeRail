# RupeeRail web app

A minimal Material Design PWA: connect a browser wallet, scan a UPI QR, sign, and follow the payment. It's phone-first, works offline as an app shell, and follows the device's light or dark setting.

Built with React, Vite and MUI. Wallet access is plain EIP-1193 and EIP-6963 with no web3 library, because the backend builds every typed-data payload. To add a connector such as WalletConnect, supply another `Eip1193Provider` in `src/lib/wallet.ts`.

## Wallets on phones

Only desktop extensions and wallet in-app browsers provide a wallet to a web page. An installed PWA has none, so on a phone with no wallet the app offers **Open in MetaMask**, which runs it in MetaMask's browser. Payments have to happen there, whether or not the PWA is installed.

## Develop

```bash
npm install
npm run dev        # http://localhost:5173, proxying the API to the deployed backend
```

Set `RUPEERAIL_API_URL` to proxy to another backend. Camera access needs HTTPS or localhost.

## Test

```bash
npm test                               # unit tests
npx playwright test                    # real Chrome at phone size, with a scripted API and a test wallet
E2E_SCREENSHOTS=1 npx playwright test  # also saves screens to e2e/screenshots
```

`e2e/live.spec.ts` sends real testnet transactions and only runs when its environment variables are set. See the top of that file.

## Deploy

The app and API share one hostname, so the app must not use a client-side path that the API owns: `/health`, `/config`, `/account`, `/quote`, `/offramp`, `/offramps`, `/dispute`, `/events`, `/onramp`, `/admin`, `/arkiv` or `/agent-api`. If you add an API path, also add it to `deploy/Caddyfile` and to `API_PATHS` in `vite.config.ts`.

`npm run build` runs `scripts/pack-extension.mjs`, which builds `extension/` and zips it to `public/downloads/rupeerail-agents-extension.zip` (not in git), so each deploy ships the extension from the same checkout. The Agents Hub offers that zip for sideloading. The build needs the `zip` command, and fails rather than ship a page with a dead download. `npm run dev` doesn't make the zip, so run `npm run pack:extension` first to try the download there.

The Buy tab appears only when the backend's `/config` reports `onramp.rail` as `razorpay`, so no separate build is needed to launch it. The manual rail stays hidden because its UPI ID is a placeholder until it's set.

The CSP's `connect-src` in `deploy/Caddyfile` lists every host the browser calls directly: the Arc RPC and Circle's API, for the swap. `e2e/csp.spec.ts` runs the swap under that exact policy. Reload Caddy whenever it changes.

```bash
npm run build
rsync -az --delete --rsync-path="sudo rsync" --chown=root:root dist/ <host>:/var/www/rupeerail/
rsync -az --rsync-path="sudo rsync" --chown=root:root deploy/Caddyfile <host>:/etc/caddy/Caddyfile
ssh <host> 'sudo systemctl reload caddy'
```

### The Arkiv read service

`/arkiv` on the same hostname is `services/arkiv-board/src/reader-server.ts`, a separate service that only reads. It needs Node 22.10 or later, which the server keeps apart from anything else, and an access key. It holds no signing key. Set it up before deploying a Caddyfile that points at it. Run this from `services/arkiv-board`:

```bash
ssh <host> 'cd /tmp && curl -fsSLO https://nodejs.org/dist/v22.20.0/node-v22.20.0-linux-x64.tar.xz \
  && sudo mkdir -p /opt/rupeerail/node /opt/rupeerail/arkiv-board \
  && sudo tar -xJf node-v22.20.0-linux-x64.tar.xz -C /opt/rupeerail/node --strip-components=1'
rsync -az --delete --rsync-path="sudo rsync" --chown=root:root --include='src/' --include='src/arkiv.ts' --include='src/ops.ts' \
  --include='src/reads.ts' --include='src/reader-server.ts' --include='src/schema.ts' --include='src/verify.ts' \
  --include='package.json' --include='package-lock.json' --exclude='*' ./ <host>:/opt/rupeerail/arkiv-board/
ssh <host> 'cd /opt/rupeerail/arkiv-board && sudo PATH=/opt/rupeerail/node/bin:$PATH npm ci --omit=dev'
grep '^ARKIV_ACCESS_KEY=' .env | ssh <host> 'sudo install -m 640 -o root -g rupeerail /dev/stdin /etc/rupeerail/arkiv.env'
rsync -az --rsync-path="sudo rsync" --chown=root:root deploy/rupeerail-arkiv.service <host>:/etc/systemd/system/
ssh <host> 'sudo systemctl daemon-reload && sudo systemctl enable --now rupeerail-arkiv'
```

The publisher, which holds the signing key, runs beside the agent server and never on this host.

## Buying tokens (the Buy tab)

The user enters an INR amount, reviews the rate and pays on Razorpay's hosted page. That page is a full-page visit, not a popup, so the CSP stays `'self'`-only. Razorpay sends the browser to the API's callback, which redirects to `/?onramp=<orderId>&payment=received`. The app reads those parameters once, removes them from the address bar and polls `GET /onramp/{id}` until the tokens are sent. It's a query on `/` because `/onramp/...` belongs to the API. The last 10 purchases are kept in `localStorage` for the "Recent purchases" list.

"Payment received" only means Razorpay's redirect checked out. Tokens are sent after the server confirms the payment with Razorpay itself.

## What "Payment processed" means

`COMPLETED` means the operator reported the INR payout, which the chain can't verify. The app says "processed" rather than claiming the merchant has the money, and offers a dispute for 24 hours.
