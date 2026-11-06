/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const BACKEND = process.env.RUPEERAIL_API_URL ?? "https://rupee-rail.indiasouthcentral.cloudapp.azure.com";
const API_PATHS = /^\/(health|config|account|quote|offramps?|dispute|events|onramp|admin)(\/|$)/;

const proxy = {
  "^/(health|config|account|quote|offramps?|dispute|events|onramp|admin)(/|$)": {
    target: BACKEND,
    changeOrigin: true,
    secure: true,
  },
};

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      manifest: {
        id: "/",
        name: "RupeeRail",
        short_name: "RupeeRail",
        description: "Scan any UPI QR and pay in stablecoins, with no gas.",
        start_url: "/",
        scope: "/",
        display: "standalone",
        orientation: "portrait",
        background_color: "#f8f9ff",
        theme_color: "#f8f9ff",
        categories: ["finance"],
        icons: [
          { src: "/pwa-192x192.png", sizes: "192x192", type: "image/png" },
          { src: "/pwa-512x512.png", sizes: "512x512", type: "image/png" },
          { src: "/maskable-icon-512x512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        // Only the app shell is cached. API responses must always come from the network.
        globPatterns: ["**/*.{js,css,html,woff2,svg,png}"],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [API_PATHS],
      },
    }),
  ],
  server: { proxy },
  preview: { proxy },
  build: { chunkSizeWarningLimit: 700 },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"], globals: true },
});
