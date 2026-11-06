import { createTheme } from "@mui/material/styles";

const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";

/** Confirms a press the moment it happens. Colours ease, and only the transform uses the stronger curve. */
function pressable(scale: number) {
  return {
    transition: `transform 160ms ${EASE_OUT}, background-color 150ms ease, border-color 150ms ease, color 150ms ease`,
    "&:active:not(.Mui-disabled)": { transform: `scale(${scale})` },
  };
}

/** Material 3 colour roles and shapes on top of MUI, following the device's light or dark setting. */
export const theme = createTheme({
  cssVariables: { colorSchemeSelector: "media" },
  colorSchemes: {
    light: {
      palette: {
        primary: { main: "#1b5e9e", contrastText: "#ffffff" },
        secondary: { main: "#d3e4ff", contrastText: "#001c38" },
        background: { default: "#f8f9ff", paper: "#ffffff" },
        text: { primary: "#191c20", secondary: "#43474e" },
        divider: "#dfe2eb",
        success: { main: "#1b6e3c" },
        warning: { main: "#8a5100" },
        error: { main: "#ba1a1a" },
      },
    },
    dark: {
      palette: {
        primary: { main: "#a3c9ff", contrastText: "#00315b" },
        secondary: { main: "#2c4770", contrastText: "#d3e4ff" },
        background: { default: "#101418", paper: "#1d2024" },
        text: { primary: "#e1e2e8", secondary: "#c3c6cf" },
        divider: "#33353a",
        success: { main: "#7dd99a" },
        warning: { main: "#ffb95f" },
        error: { main: "#ffb4ab" },
      },
    },
  },
  shape: { borderRadius: 16 },
  typography: {
    fontFamily: "Roboto, system-ui, sans-serif",
    // Money and countdowns: digits keep their width, so an amount doesn't shift as it changes.
    allVariants: { fontVariantNumeric: "tabular-nums" },
    button: { textTransform: "none", fontWeight: 500, letterSpacing: 0.1 },
    h4: { fontWeight: 400, letterSpacing: 0 },
    h5: { fontWeight: 400 },
    h6: { fontWeight: 500, fontSize: "1.125rem" },
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: { root: { borderRadius: 999, minHeight: 48, paddingInline: 24, ...pressable(0.97) } },
    },
    // A ripple runs after the press and keeps going. A small scale answers at once, and the ring below replaces its focus cue.
    MuiButtonBase: {
      defaultProps: { disableRipple: true },
      styleOverrides: {
        root: {
          "&.Mui-focusVisible": { outline: "2px solid var(--mui-palette-primary-main)", outlineOffset: 2 },
        },
      },
    },
    MuiIconButton: { styleOverrides: { root: pressable(0.92) } },
    MuiToggleButton: { styleOverrides: { root: pressable(0.98) } },
    MuiChip: { styleOverrides: { clickable: pressable(0.97) } },
    MuiCircularProgress: { styleOverrides: { root: { animationDuration: "900ms" } } },
    MuiTooltip: { defaultProps: { enterDelay: 400, enterNextDelay: 0, slotProps: { transition: { timeout: 125 } } } },
    MuiDialog: {
      defaultProps: { transitionDuration: { enter: 200, exit: 130 } },
      styleOverrides: { paper: { borderRadius: 28 } },
    },
    MuiCard: {
      defaultProps: { variant: "outlined" },
      styleOverrides: { root: { borderRadius: 16 } },
    },
    MuiPaper: { styleOverrides: { rounded: { borderRadius: 16 } } },
    MuiTextField: { defaultProps: { fullWidth: true } },
    MuiBottomNavigationAction: {
      styleOverrides: {
        root: {
          minHeight: 64,
          // Used constantly, so nothing here moves: MUI's default animates the label's font size and the padding.
          paddingInline: 12,
          transition: `transform 160ms ${EASE_OUT}, color 120ms ease`,
          "&:active": { transform: "scale(0.97)" },
          "& .MuiSvgIcon-root": {
            boxSizing: "content-box",
            padding: "4px 18px",
            marginBottom: 2,
            borderRadius: 999,
            transition: "background-color 120ms ease",
          },
          "&.Mui-selected": { paddingInline: 12 },
          "&.Mui-selected .MuiSvgIcon-root": { backgroundColor: "var(--mui-palette-secondary-main)" },
          "&.Mui-selected, &.Mui-selected .MuiBottomNavigationAction-label": { color: "var(--mui-palette-text-primary)" },
        },
        label: { fontSize: "0.75rem", transition: "none", "&.Mui-selected": { fontSize: "0.75rem", fontWeight: 500 } },
      },
    },
  },
});
