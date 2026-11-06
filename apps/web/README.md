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

The app and API share one hostname, so the app must not use a client-side path that the API owns: `/health`, `/config`, `/account`, `/quote`, `/offramp`, `/offramps`, `/dispute`, `/events`, `/onramp` or `/admin`. If you add an API path, also add it to `deploy/Caddyfile` and to `API_PATHS` in `vite.config.ts`.

```bash
npm run build
rsync -az --delete --rsync-path="sudo rsync" --chown=root:root dist/ <host>:/var/www/rupeerail/
rsync -az --rsync-path="sudo rsync" --chown=root:root deploy/Caddyfile <host>:/etc/caddy/Caddyfile
ssh <host> 'sudo systemctl reload caddy'
```

## What "Payment processed" means

`COMPLETED` means the operator reported the INR payout, which the chain can't verify. The app says "processed" rather than claiming the merchant has the money, and offers a dispute for 24 hours.
