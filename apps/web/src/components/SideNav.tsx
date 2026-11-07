import Box from "@mui/material/Box";
import ButtonBase from "@mui/material/ButtonBase";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { ReactNode } from "react";

interface Props<T extends string> {
  tabs: { value: T; label: string; icon: ReactNode }[];
  value: T;
  onChange: (value: T) => void;
}

/**
 * Material 3's extended navigation rail, for wide screens: each destination shows its icon and label on one line.
 * It's used constantly, so nothing moves when the selection changes, and only a press answers with motion.
 */
export default function SideNav<T extends string>({ tabs, value, onChange }: Props<T>) {
  return (
    <Box
      component="nav"
      aria-label="Main"
      sx={{
        position: "sticky",
        top: 0,
        flexShrink: 0,
        boxSizing: "border-box",
        width: 280,
        height: "100dvh",
        p: 1.5,
        bgcolor: "background.surface",
        borderTopRightRadius: 28,
        borderBottomRightRadius: 28,
      }}
    >
      <Stack direction="row" spacing={1.5} sx={{ alignItems: "center", px: 2, pt: 1.5, pb: 3 }}>
        <Box component="img" src="/icon.svg" alt="" sx={{ width: 32, height: 32 }} />
        <Typography variant="h6" component="h1" sx={{ fontSize: "1.375rem", letterSpacing: "-0.01em" }}>RupeeRail</Typography>
      </Stack>
      <Stack spacing={0.5}>
        {tabs.map((tab) => {
          const selected = tab.value === value;
          return (
            <ButtonBase
              key={tab.value}
              onClick={() => onChange(tab.value)}
              aria-current={selected ? "page" : undefined}
              sx={{
                justifyContent: "flex-start",
                gap: 1.5,
                minHeight: 56,
                px: 2,
                borderRadius: 999,
                fontSize: "0.9375rem",
                fontWeight: selected ? 500 : 400,
                color: selected ? "secondary.contrastText" : "text.secondary",
                bgcolor: selected ? "secondary.main" : "transparent",
                transition: "transform 160ms var(--ease-out), border-radius 160ms var(--ease-out), background-color 120ms ease, color 120ms ease",
                "@media (hover: hover) and (pointer: fine)": {
                  "&:hover": { bgcolor: selected ? "secondary.main" : "background.surfacePress", color: selected ? "secondary.contrastText" : "text.primary" },
                },
                "&:active": { transform: "scale(0.98)", borderRadius: "18px" },
              }}
            >
              {tab.icon}
              <span>{tab.label}</span>
            </ButtonBase>
          );
        })}
      </Stack>
    </Box>
  );
}
