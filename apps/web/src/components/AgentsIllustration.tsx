import Box from "@mui/material/Box";
import { cookiePath, sparklePath } from "./ScanIllustration";

const PRIMARY = "var(--mui-palette-primary-main)";
const ON_PRIMARY = "var(--mui-palette-primary-contrastText)";
const TONAL = "var(--mui-palette-secondary-main)";
const PAPER = "var(--mui-palette-background-paper)";
const INK = "var(--mui-palette-text-primary)";
const ACCENT = "var(--rr-accent)";
const ACCENT_CONTAINER = "var(--rr-accent-container)";

/** An agent holding a paid receipt, drawn to sit beside the scan illustration. Colours come from the theme, so it follows dark mode. */
export default function AgentsIllustration() {
  return (
    <Box
      component="svg"
      viewBox="0 0 320 220"
      aria-hidden="true"
      data-testid="agents-illustration"
      sx={{ display: "block", alignSelf: "center", width: "100%", maxWidth: 300, height: "auto" }}
    >
      <path d={cookiePath(160, 110, 100, 12, 0.055)} fill={TONAL} />

      <path d="M70 128C84 44 190 8 232 44" fill="none" stroke={PRIMARY} strokeWidth={2.5} strokeLinecap="round" strokeDasharray="1 8" />

      <g transform="rotate(-5 160 110)">
        <path d="M160 36V22" stroke={PRIMARY} strokeWidth={4} strokeLinecap="round" />
        <circle cx={160} cy={18} r={6} fill={ACCENT} />

        <rect x={92} y={66} width={12} height={28} rx={6} fill={PRIMARY} />
        <rect x={216} y={66} width={12} height={28} rx={6} fill={PRIMARY} />
        <rect x={102} y={36} width={116} height={86} rx={28} fill={PAPER} stroke={PRIMARY} strokeWidth={3} />
        <circle cx={136} cy={72} r={9} fill={INK} />
        <circle cx={184} cy={72} r={9} fill={INK} />
        <path d="M140 94Q160 110 180 94" fill="none" stroke={INK} strokeWidth={4} strokeLinecap="round" />

        <rect x={112} y={130} width={96} height={72} rx={16} fill={PAPER} stroke={PRIMARY} strokeWidth={3} />
        <circle cx={134} cy={153} r={11} fill={ACCENT} />
        <path d="M129 153L133 157L140 149" fill="none" stroke={PAPER} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" />
        <rect x={152} y={150} width={42} height={7} rx={3.5} fill={INK} />
        <rect x={124} y={178} width={72} height={10} rx={5} fill={PRIMARY} />
      </g>

      <circle cx={258} cy={62} r={29} fill={ACCENT_CONTAINER} />
      <text x={258} y={72} textAnchor="middle" fontSize={30} fontWeight={500} fill={ACCENT}>₹</text>

      <circle cx={62} cy={150} r={26} fill={PRIMARY} />
      <text x={62} y={159} textAnchor="middle" fontSize={26} fontWeight={500} fill={ON_PRIMARY}>$</text>

      <path d={sparklePath(56, 56, 13)} fill={ACCENT} />
      <path d={sparklePath(270, 156, 10)} fill={PRIMARY} />
      <rect x={272} y={108} width={16} height={16} rx={5} fill={ACCENT_CONTAINER} transform="rotate(18 280 116)" />
      <circle cx={30} cy={104} r={6} fill={TONAL} />
    </Box>
  );
}
