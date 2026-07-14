import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

// Design system: IBM Plex Sans for UI/headings/body, IBM Plex Mono for every
// number, ticker, and tabular data point (tabular figures enabled globally).
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
});
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "East Equity Agent",
  description:
    "A fully transparent AI swing-trading agent. Long-only equities in the AI supply chain, every decision published with full reasoning.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable}`}>
      <body className="font-[family-name:var(--font-sans)] min-h-[100dvh]">
        {children}
      </body>
    </html>
  );
}
