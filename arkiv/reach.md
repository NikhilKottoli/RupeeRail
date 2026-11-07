# Reach

## Who it's for

- **People running agents that spend their money.** RupeeRail's agents order and pay on real sites, and this shows the owner what each one did, in a form they don't have to take the agent's word for.
- **Anyone deciding whether to trust an agent or its template.** They can read an agent's paid and failed payments without an account or our permission.
- **Other apps.** The schema is public, so another wallet or directory can read the same records without asking us.

## Can they try it today?

- **Without installing anything:** [the public Agents page](https://rupee-rail.indiasouthcentral.cloudapp.azure.com/agents) needs no wallet.
- **With a free access key:** `ARKIV_ACCESS_KEY=<key> npm run verify` in `services/arkiv-board` reads the same records without our servers.
- **To publish a record:** this takes the extension, the agent server and a phone to make the payment, so people can't yet do this without setting those up.

## What's real so far

These are real numbers, and they're small. One agent is published, with 2 receipts: a ₹232 payment on a real shopping site from the agent's own wallet, which the phone reported as paid, and a failed ₹1 attempt that stays visible. All payments are from testnet tokens, while the rupees paid to the merchant are real.

## After Mumbai

We'll keep building RupeeRail after Mumbai. Its first users are people who want to spend stablecoins on UPI payments across India's heavily UPI-based economy, such as at a restaurant that takes UPI but has no point-of-sale system. That merchant needs nothing new: they get rupees through the UPI QR they already have. As agents start paying for those people, a public track record is how they can see what an agent has done.
