import Box from "@mui/material/Box";
import ListItemButton from "@mui/material/ListItemButton";
import Stack from "@mui/material/Stack";
import type { SxProps, Theme } from "@mui/material/styles";
import Typography from "@mui/material/Typography";
import type { ComponentProps, ReactNode } from "react";

const OUTER = "28px";
const INNER = "6px";

/**
 * A Material 3 Expressive list: one group, with its outer corners very round and its inner corners nearly square.
 * The row inside each item carries the colour and shape, so a press can reshape a row without touching its neighbours.
 */
export function GroupedList({ children, ...rest }: ComponentProps<"ul">) {
  return (
    <Box
      component="ul"
      {...rest}
      sx={{
        display: "flex",
        flexDirection: "column",
        gap: "3px",
        m: 0,
        p: 0,
        listStyle: "none",
        "& > li > *": { bgcolor: "background.surface", borderRadius: INNER },
        "& > li:first-of-type > *": { borderTopLeftRadius: OUTER, borderTopRightRadius: OUTER },
        "& > li:last-of-type > *": { borderBottomLeftRadius: OUTER, borderBottomRightRadius: OUTER },
        "& > li > .MuiListItemButton-root": {
          transition: "transform 160ms var(--ease-out), border-radius 160ms var(--ease-out), background-color 150ms ease",
          "@media (hover: hover) and (pointer: fine)": { "&:hover": { bgcolor: "background.surfacePress" } },
          "&:active": { transform: "scale(0.985)", borderRadius: "18px", bgcolor: "background.surfacePress" },
        },
      }}
    >
      {children}
    </Box>
  );
}

/** A section of a group that holds anything: a headline figure, a status, or a form. */
export function GroupedBlock({ children }: { children: ReactNode }) {
  return <li><Box sx={{ px: "20px", py: "18px" }}>{children}</Box></li>;
}

/** One label and value, such as the rate on a payment. */
export function GroupedDetail({ label, value }: { label: string; value: string }) {
  return (
    <li>
      <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center", gap: 2, px: "20px", py: "14px" }}>
        <Typography variant="body2" color="text.secondary">{label}</Typography>
        <Typography variant="body2" sx={{ fontWeight: 500, textAlign: "right", overflowWrap: "anywhere" }}>{value}</Typography>
      </Stack>
    </li>
  );
}

/** A row that opens something. */
export function GroupedButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <li>
      <ListItemButton onClick={onClick} sx={{ minHeight: 76, gap: 1.5, px: "20px", py: "14px" }}>
        {children}
      </ListItemButton>
    </li>
  );
}

/** A figure that leads a row, such as a payment's amount. */
export function RowAmount({ children }: { children: ReactNode }) {
  return <Typography sx={{ fontSize: "1.25rem", lineHeight: 1.1, fontWeight: 500, letterSpacing: "-0.02em" }}>{children}</Typography>;
}

/** A primary and a secondary action side by side. The primary takes the room its longer label needs. */
export function ActionRow({ children }: { children: ReactNode }) {
  return (
    <Stack
      direction="row"
      spacing={1}
      sx={{ "& > *": { minWidth: 0 }, "& > :first-of-type": { flex: "1 1 auto" }, "& > :last-of-type:not(:first-of-type)": { flex: "0 0 auto" } }}
    >
      {children}
    </Stack>
  );
}

/** The main action on a screen: short, tall and centered, rather than a wide bar. */
export const heroAction = {
  alignSelf: "center",
  minWidth: 176,
  minHeight: 64,
  px: "30px",
  borderRadius: "26px",
  fontSize: "1rem",
  "&:active:not(.Mui-disabled)": { borderRadius: "20px" },
} satisfies SxProps<Theme>;
