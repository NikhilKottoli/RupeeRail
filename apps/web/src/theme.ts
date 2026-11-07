import { createTheme } from "@mui/material/styles";

declare module "@mui/material/styles" {
  interface TypeBackground {
    /** Material 3's surface container: what grouped lists, the sidebar and the bottom bar sit on. */
    surface: string;
    surfacePress: string;
  }
}

const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";
const TONAL = "var(--mui-palette-secondary-main)";
const ON_TONAL = "var(--mui-palette-secondary-contrastText)";
const TONAL_HOVER = `color-mix(in srgb, ${TONAL} 92%, ${ON_TONAL})`;

/**
 * Confirms a press the moment it happens. Colours ease, and only the shape uses the stronger curve.
 * Material 3 Expressive squares a pressed button up a little, so a pill also gets a pressed radius.
 */
function pressable(scale: number, pressedRadius?: number) {
  return {
    transition: `transform 160ms ${EASE_OUT}, border-radius 160ms ${EASE_OUT}, background-color 150ms ease, border-color 150ms ease, color 150ms ease`,
    "&:active:not(.Mui-disabled)": {
      transform: `scale(${scale})`,
      ...(pressedRadius === undefined ? {} : { borderRadius: pressedRadius }),
    },
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
        background: { default: "#f8f9ff", paper: "#ffffff", surface: "#edf0f8", surfacePress: "#e2e6f1" },
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
        background: { default: "#101418", paper: "#1d2024", surface: "#1d2126", surfacePress: "#272c33" },
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
    h3: { fontSize: "2.5rem", lineHeight: 1.1, fontWeight: 500, letterSpacing: "-0.03em" },
    h4: { fontSize: "2.125rem", lineHeight: 1.1, fontWeight: 500, letterSpacing: "-0.02em" },
    h5: { fontSize: "1.625rem", lineHeight: 1.15, fontWeight: 500, letterSpacing: "-0.015em" },
    h6: { fontWeight: 500, fontSize: "1.125rem" },
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: {
          borderRadius: 999,
          minHeight: 52,
          paddingInline: 24,
          ...pressable(0.97, 18),
          // The app's secondary action is a tonal button. A dispute keeps its outline, because it isn't an everyday action.
          variants: [
            {
              props: { variant: "outlined", color: "primary" },
              style: {
                border: "1px solid transparent",
                backgroundColor: TONAL,
                color: ON_TONAL,
                // Only a real pointer hovers. On a touch screen a hover colour would stick after the tap.
                "&:hover": { borderColor: "transparent", backgroundColor: TONAL },
                "@media (hover: hover) and (pointer: fine)": { "&:hover": { backgroundColor: TONAL_HOVER } },
                "&.Mui-disabled": {
                  borderColor: "transparent",
                  backgroundColor: "var(--mui-palette-action-disabledBackground)",
                  color: "var(--mui-palette-action-disabled)",
                },
              },
            },
          ],
        },
        sizeLarge: { minHeight: 56, paddingInline: 28, fontSize: "1rem" },
      },
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
    // A connected button group: the outer corners are very round, the inner ones nearly square, like a grouped list.
    MuiToggleButtonGroup: {
      styleOverrides: {
        root: {
          gap: 3,
          "&& .MuiToggleButton-root": { margin: 0, border: 0, borderRadius: 6 },
          "&& .MuiToggleButton-root.MuiToggleButtonGroup-firstButton": { borderTopLeftRadius: 28, borderBottomLeftRadius: 28 },
          "&& .MuiToggleButton-root.MuiToggleButtonGroup-lastButton": { borderTopRightRadius: 28, borderBottomRightRadius: 28 },
        },
      },
    },
    MuiToggleButton: {
      styleOverrides: {
        root: {
          ...pressable(0.98),
          minHeight: 56,
          paddingInline: 16,
          textTransform: "none",
          backgroundColor: "var(--mui-palette-background-default)",
          "&:hover": { backgroundColor: "var(--mui-palette-background-default)" },
          "@media (hover: hover) and (pointer: fine)": { "&:hover": { backgroundColor: "var(--mui-palette-background-surfacePress)" } },
          "&&.Mui-selected, &&.Mui-selected:hover": { backgroundColor: TONAL, color: ON_TONAL },
        },
      },
    },
    MuiOutlinedInput: { styleOverrides: { root: { backgroundColor: "var(--mui-palette-background-default)" } } },
    MuiChip: { styleOverrides: { clickable: pressable(0.97) } },
    MuiCircularProgress: { styleOverrides: { root: { animationDuration: "900ms" } } },
    MuiTooltip: { defaultProps: { enterDelay: 400, enterNextDelay: 0, slotProps: { transition: { timeout: 125 } } } },
    MuiDialog: {
      defaultProps: { transitionDuration: { enter: 200, exit: 130 } },
      styleOverrides: { paper: { borderRadius: 28 } },
    },
    MuiPaper: { styleOverrides: { rounded: { borderRadius: 16 } } },
    MuiTextField: { defaultProps: { fullWidth: true } },
    MuiBottomNavigation: { styleOverrides: { root: { height: 72, backgroundColor: "transparent" } } },
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
