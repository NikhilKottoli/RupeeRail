import ExpandMore from "@mui/icons-material/ExpandMore";
import Accordion from "@mui/material/Accordion";
import AccordionDetails from "@mui/material/AccordionDetails";
import AccordionSummary from "@mui/material/AccordionSummary";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { AppConfig } from "../lib/api";
import { formatInr } from "../lib/format";

interface Props {
  /** Shown when the config has loaded. The page works without it, so it stays readable when the backend is down. */
  limits?: AppConfig["limits"];
}

interface Item {
  id: string;
  question: string;
  answer: string[];
  rupeerail: string[];
}

const LEGAL: Item[] = [
  {
    id: "legality",
    question: "Is operating a USDC-to-INR off-ramp or cryptocurrency platform illegal in India?",
    answer: [
      "No. The operation of a USDC-to-INR off-ramp or cryptocurrency conversion platform is legal under Indian law. No statutory ban or legislative prohibition exists against virtual digital asset (VDA) transactions or off-ramp services in India.",
      "In Internet and Mobile Association of India (IAMAI) v. Reserve Bank of India (2020) 10 SCC 274, the Supreme Court of India set aside the RBI Circular dated 06.04.2018 that prohibited regulated entities from providing banking services to crypto businesses. The Court held that, in the absence of a legislative ban, trading and dealing in virtual digital assets is a constitutionally protected trade under Article 19(1)(g) of the Constitution of India.",
      "Indian law distinguishes between the absence of a standalone regulatory statute and a legal prohibition. While a dedicated statute for VDAs is yet to be enacted, virtual digital assets are recognised, regulated and taxed under the Income Tax Act, 1961 and the Prevention of Money Laundering Act, 2002.",
    ],
    rupeerail: [
      "RupeeRail converts a stablecoin (USDC on Arc, USDT on Whitechain) into a rupee payment to a UPI merchant, which is the kind of VDA-to-fiat service this answer covers.",
      "Today RupeeRail runs only on test networks, with test tokens that have no market value.",
    ],
  },
  {
    id: "registration",
    question: "What regulatory registrations are required to operate legally?",
    answer: [
      "Operating a USDC-to-INR off-ramp requires registration with the Financial Intelligence Unit - India (FIU-IND) under the Ministry of Finance. Under Notification No. S.O. 1056(E) dated 07.03.2023 issued under the Prevention of Money Laundering Act, 2002 (PMLA), entities facilitating exchange between VDAs and fiat currencies, or between different VDAs, are Reporting Entities operating as Virtual Digital Asset Service Providers (VDASPs).",
      "A registered VDASP must comply with the Prevention of Money Laundering (Maintenance of Records) Rules, 2005, including mandatory Know Your Customer (KYC) and Anti-Money Laundering (AML) checks before processing user transactions.",
      "The entity must appoint a Designated Director and a Principal Officer responsible for statutory compliance and reporting to FIU-IND, and must file regular returns, including Cash Transaction Reports (CTR), Suspicious Transaction Reports (STR) and Network Transaction Reports (NTR).",
    ],
    rupeerail: [
      "RupeeRail is not yet registered with FIU-IND, and it doesn't ask users for KYC. You connect a wallet and pay. Registration, KYC checks and a named Principal Officer are needed before it handles real money.",
      "What it does have today: every payment is checked against a per-payment and a daily rupee limit for each wallet, requests are rate-limited, and the backend refunds any locked payment that doesn't match the quote it issued or carries an invalid UPI QR.",
    ],
  },
  {
    id: "tax",
    question: "What is the direct tax compliance framework for VDA off-ramps?",
    answer: [
      "Direct tax on VDA off-ramp transactions is governed by the Income Tax Act, 1961. Section 115BBH levies income tax at 30% (plus applicable surcharge and cess) on income from the transfer of any Virtual Digital Asset, including USDC.",
      "Section 194S requires tax deduction at source (TDS) of 1% on payments made to an Indian resident for the transfer of a VDA where the value exceeds the statutory thresholds. The platform operator, or the person paying the consideration, must deduct the 1% TDS and deposit it with the Income Tax Department through Form 26QE.",
    ],
    rupeerail: [
      "Paying a merchant with USDC or USDT transfers a VDA, so any gain over what you paid for those tokens may be taxable. The History tab lists each payment's rupee amount and date, and opening one shows the tokens spent, to help with your records.",
      "RupeeRail's quotes don't deduct TDS yet. The rate you see includes only the conversion spread, and there's no network fee for you to pay.",
    ],
  },
  {
    id: "banking",
    question: "How are banking services and RBI directives managed?",
    answer: [
      "Banking operations for VDA off-ramps are permissible. After the 2018 circular was quashed, the Reserve Bank of India issued Circular RBI/2021-22/45 dated 31.05.2021, advising banks and financial institutions not to cite the set-aside 2018 circular to deny banking services to entities dealing in VDAs.",
      "Risk management requires distinguishing statutory illegality from operational risk. Operational risk mainly comes from bank account freezes under Section 106 of the Bharatiya Nagarik Suraksha Sanhita, 2023 (BNSS) or Section 102 of the Code of Criminal Procedure, 1973 (CrPC), initiated by cybercrime police because of tainted peer-to-peer (P2P) fiat flows. Such freezes are investigations into specific suspicious funds and are not a ban on off-ramp operations.",
    ],
    rupeerail: [
      "RupeeRail doesn't trade peer-to-peer with other people. The rupees a merchant receives come from RupeeRail's own UPI account, and your tokens stay locked in an on-chain vault until the payment settles, which limits exposure to tainted P2P funds.",
      "If a payment goes wrong, you can dispute it from the History tab for 24 hours after it's processed. A dispute that isn't resolved within 7 days returns your tokens automatically.",
    ],
  },
  {
    id: "fema-certin",
    question: "What are the foreign exchange (FEMA) and cybersecurity (CERT-In) compliance requirements?",
    answer: [
      "Foreign exchange compliance is governed by the Foreign Exchange Management Act, 1999 (FEMA). Domestic USDC-to-INR conversions between resident entities in India fall under domestic payment regulations. Any cross-border remittance, foreign currency inflow or outflow, or transaction with non-resident counterparties must follow FEMA rules, Liberalised Remittance Scheme (LRS) limits and Authorised Dealer Category-I bank reporting requirements.",
      "Cybersecurity compliance is governed by the Indian Computer Emergency Response Team (CERT-In) Directions dated 28.04.2022. VDA service providers must report cybersecurity incidents to CERT-In within 6 hours of noticing them, and must keep subscriber KYC information and transaction logs for 5 years.",
    ],
    rupeerail: [
      "RupeeRail pays only Indian UPI merchants, in rupees. It doesn't yet check where a wallet's owner lives, which matters for FEMA once real funds are involved.",
      "Your merchant's UPI ID, name and QR are kept off the blockchain. Only a one-way fingerprint of the QR is recorded on-chain, and the full details stay on RupeeRail's server. A 5-year record-keeping policy and a CERT-In incident process are still to be put in place.",
    ],
  },
];

function Paragraphs({ lines }: { lines: string[] }) {
  return (
    <Stack spacing={1.5}>
      {lines.map((line) => <Typography key={line} variant="body2">{line}</Typography>)}
    </Stack>
  );
}

export default function ComplianceScreen({ limits }: Props) {
  return (
    <Stack spacing={3} sx={{ pt: 2 }}>
      <Stack spacing={1}>
        <Typography variant="h4" component="h2">Compliance and FAQ</Typography>
        <Typography color="text.secondary">
          The legal framework for turning stablecoins into rupees in India, and where RupeeRail stands on each part.
        </Typography>
      </Stack>

      <Alert severity="info">
        RupeeRail is a prototype running on Arc testnet and Whitechain Sepolia. It isn't yet a registered virtual digital asset
        service provider, so don't use it with real funds.
        {limits && ` Each payment must be between ${formatInr(limits.minInrPaise)} and ${formatInr(limits.maxInrPaise)}, and each wallet can pay up to ${formatInr(limits.dailyUserInrPaise)} a day.`}
      </Alert>

      <Box>
        {LEGAL.map((item, index) => (
          <Accordion key={item.id} disableGutters variant="outlined" sx={{ "&:not(:first-of-type)": { borderTop: 0 } }}>
            <AccordionSummary expandIcon={<ExpandMore />} aria-controls={`${item.id}-content`} id={`${item.id}-header`}>
              <Typography sx={{ fontWeight: 500 }}>{`${index + 1}. ${item.question}`}</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={2}>
                <Paragraphs lines={item.answer} />
                <Box sx={{ bgcolor: "action.hover", borderRadius: 2, p: 2 }}>
                  <Typography variant="subtitle2" component="h4" sx={{ mb: 1 }}>How this applies to RupeeRail</Typography>
                  <Paragraphs lines={item.rupeerail} />
                </Box>
              </Stack>
            </AccordionDetails>
          </Accordion>
        ))}
      </Box>

      <Stack spacing={1}>
        <Typography variant="subtitle2" component="h3">Disclaimer</Typography>
        <Typography variant="body2" color="text.secondary">
          The operation of a USDC-to-INR VDA off-ramp in India is legal and supported by judicial precedent and statutory
          reporting frameworks, subject to full compliance with FIU-IND registration, AML and KYC norms, tax deduction
          obligations, FEMA and CERT-In standards. This page is for information only and isn't legal advice. Specific
          operating structures must be reviewed against evolving statutory guidelines.
        </Typography>
      </Stack>
    </Stack>
  );
}
