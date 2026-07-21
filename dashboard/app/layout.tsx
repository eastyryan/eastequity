import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, Instrument_Serif, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// Arena (front page): Instrument Serif for display headlines, JetBrains Mono
// for everything else — the page is mono by default, serif only where it shouts.
const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-serif",
});
const jetBrains = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jet",
});

// Legacy system (/runs, /detail): IBM Plex Sans for UI/headings/body, IBM Plex
// Mono for every number, ticker, and tabular data point.
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
  // The label under the icon when the site is saved to an iOS home screen —
  // without it iOS truncates the full <title>.
  appleWebApp: { title: "East Equity" },
};

// Runs before first paint so the stored theme is on <html> by the time anything
// renders — otherwise a dark-mode visitor gets a white flash on every load.
// Sets both hooks: .dark drives the Arena tokens, [data-theme] the legacy ones.
const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem('ee-theme');var d=t==='dark';var e=document.documentElement;e.classList.toggle('dark',d);e.setAttribute('data-theme',d?'dark':'light');}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      data-theme="light"
      className={`${plexSans.variable} ${plexMono.variable} ${instrumentSerif.variable} ${jetBrains.variable}`}
      // The script above mutates class/data-theme before hydration; without this
      // React warns that the server markup and the DOM disagree.
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="font-[family-name:var(--font-sans)] min-h-[100dvh]">{children}</body>
    </html>
  );
}
