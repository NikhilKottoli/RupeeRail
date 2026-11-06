import Box from "@mui/material/Box";

const PRIMARY = "var(--mui-palette-primary-main)";
const ON_PRIMARY = "var(--mui-palette-primary-contrastText)";
const TONAL = "var(--mui-palette-secondary-main)";
const PAPER = "var(--mui-palette-background-paper)";
const INK = "var(--mui-palette-text-primary)";
const ACCENT = "var(--rr-accent)";
const ACCENT_CONTAINER = "var(--rr-accent-container)";

/** Material 3 Expressive's scalloped "cookie" shape, as an SVG path. */
function cookiePath(cx: number, cy: number, radius: number, lobes: number, depth: number) {
  const points = Array.from({ length: 180 }, (_, i) => {
    const angle = (i / 180) * Math.PI * 2;
    const r = radius * (1 + depth * Math.cos(lobes * angle));
    return `${(cx + r * Math.cos(angle)).toFixed(1)} ${(cy + r * Math.sin(angle)).toFixed(1)}`;
  });
  return `M${points.join("L")}Z`;
}

function sparklePath(cx: number, cy: number, r: number) {
  const k = r * 0.16;
  return `M${cx} ${cy - r}Q${cx + k} ${cy - k} ${cx + r} ${cy}Q${cx + k} ${cy + k} ${cx} ${cy + r}Q${cx - k} ${cy + k} ${cx - r} ${cy}Q${cx - k} ${cy - k} ${cx} ${cy - r}Z`;
}

// Decorative, and not a QR that scans: the three corners are drawn separately, and the rest is arbitrary.
const QR_MODULES = [
  "....#.#....",
  "....##.....",
  "....#.#....",
  "....##.....",
  "##.#.##.#.#",
  "#.##.#..##.",
  ".##.#.##.#.",
  "....#.#.##.",
  "....##.#..#",
  "....#..##.#",
  "....##.#.##",
];
const MODULE = 6.4;
const QR_X = 124.8;
const QR_Y = 74.8;

function Finder({ column, row }: { column: number; row: number }) {
  const x = QR_X + column * MODULE;
  const y = QR_Y + row * MODULE;
  return (
    <>
      <rect x={x + 1.5} y={y + 1.5} width={MODULE * 3 - 3} height={MODULE * 3 - 3} rx={4.5} fill="none" stroke={INK} strokeWidth={3} />
      <rect x={x + MODULE - 0.6} y={y + MODULE - 0.6} width={MODULE + 1.2} height={MODULE + 1.2} rx={1.8} fill={INK} />
    </>
  );
}

/** A phone reading a QR, with a stablecoin on its way to becoming rupees. Colours come from the theme, so it follows dark mode. */
export default function ScanIllustration() {
  return (
    <Box
      component="svg"
      viewBox="0 0 320 220"
      aria-hidden="true"
      data-testid="scan-illustration"
      sx={{ display: "block", alignSelf: "center", width: "100%", maxWidth: 300, height: "auto" }}
    >
      <path d={cookiePath(160, 110, 100, 12, 0.055)} fill={TONAL} />

      <path d="M70 128C84 44 190 8 232 44" fill="none" stroke={PRIMARY} strokeWidth={2.5} strokeLinecap="round" strokeDasharray="1 8" />

      <g transform="rotate(-5 160 110)">
        <rect x={110} y={14} width={100} height={192} rx={22} fill={PAPER} stroke={PRIMARY} strokeWidth={3} />
        <rect x={146} y={24} width={28} height={6} rx={3} fill={PRIMARY} />

        <path
          d="M116 80V72A8 8 0 0 1 124 64H132M188 64H196A8 8 0 0 1 204 72V80M204 140V148A8 8 0 0 1 196 156H188M132 156H124A8 8 0 0 1 116 148V140"
          fill="none"
          stroke={PRIMARY}
          strokeWidth={4}
          strokeLinecap="round"
        />
        <Finder column={0} row={0} />
        <Finder column={8} row={0} />
        <Finder column={0} row={8} />
        {QR_MODULES.flatMap((line, row) =>
          [...line].map((cell, column) => cell === "#" && (
            <rect key={`${row}-${column}`} x={QR_X + column * MODULE + 0.5} y={QR_Y + row * MODULE + 0.5} width={MODULE - 1} height={MODULE - 1} rx={1.4} fill={INK} />
          )),
        )}
        <rect x={112} y={108} width={96} height={4} rx={2} fill={ACCENT} />

        <rect x={130} y={170} width={60} height={12} rx={6} fill={PRIMARY} />
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
