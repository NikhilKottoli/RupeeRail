/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const BACKEND = process.env.RUPEERAIL_API_URL ?? "https://rupee-rail.indiasouthcentral.cloudapp.azure.com";
/** Libraries the app itself needs at load time or offline. Any other node_modules chunk belongs to the lazy swap path. */
const APP_LIBS = /node_modules[\\/](react|react-dom|react-is|scheduler|@mui|@emotion|@babel|stylis|clsx|prop-types|hoist-non-react-statics|react-transition-group|jsqr|@fontsource)[\\/]/;
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
        // The Circle swap chunks are loaded on demand, only when a user presses "Swap EURC to USDC". They stay out
        // of the install-time precache and are cached the first time they are used.
        globIgnores: ["**/circle-swap-*.js"],
        runtimeCaching: [{
          urlPattern: ({ url }) => /\/assets\/circle-swap-[^/]*\.js$/.test(url.pathname),
          handler: "CacheFirst",
          options: { cacheName: "circle-swap", expiration: { maxEntries: 8 } },
        }],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [API_PATHS],
      },
    }),
  ],
  server: { proxy },
  preview: { proxy },
  build: {
    chunkSizeWarningLimit: 700,
    rolldownOptions: {
      output: {
        chunkFileNames: (chunk) => {
          // A chunk made only of third-party code that the app doesn't need up front holds the lazy swap path (Circle
          // Swap Kit, its viem adapter and their dependencies). Prefixing it lets the service worker leave it out of
          // the install-time precache. Virtual bundler modules don't count.
          const files = chunk.moduleIds.filter((id) => !id.startsWith("\0") && !id.startsWith("__vite"));
          const lazyOnly = files.length > 0 && files.every((id) => id.includes("node_modules") && !APP_LIBS.test(id));
          return lazyOnly ? "assets/circle-swap-[name]-[hash].js" : "assets/[name]-[hash].js";
        },
      },
    },
  },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"], globals: true },
});
