import type { MetadataRoute } from "next";

// Android/Chrome "Add to Home Screen" reads this. iOS ignores it and uses the
// apple-icon.png next to this file instead — both have to exist or one platform
// falls back to a letter tile.
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "East Equity Agent",
    short_name: "East Equity",
    description:
      "A fully transparent AI swing-trading agent. Long-only equities in the AI supply chain, every decision published with full reasoning.",
    start_url: "/",
    display: "standalone",
    background_color: "#ffffff",
    theme_color: "#ffffff",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      // maskable lets Android crop to its own shape without letterboxing
      { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
