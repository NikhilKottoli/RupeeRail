import { createTheme } from "@mui/material/styles";

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
    button: { textTransform: "none", fontWeight: 500, letterSpacing: 0.1 },
    h4: { fontWeight: 400, letterSpacing: 0 },
    h5: { fontWeight: 400 },
    h6: { fontWeight: 500, fontSize: "1.125rem" },
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: { root: { borderRadius: 999, minHeight: 48, paddingInline: 24 } },
    },
    MuiCard: {
      defaultProps: { variant: "outlined" },
      styleOverrides: { root: { borderRadius: 16 } },
    },
    MuiPaper: { styleOverrides: { rounded: { borderRadius: 16 } } },
    MuiDialog: { styleOverrides: { paper: { borderRadius: 28 } } },
    MuiTextField: { defaultProps: { fullWidth: true } },
    MuiBottomNavigationAction: { styleOverrides: { root: { minHeight: 64 } } },
  },
});
