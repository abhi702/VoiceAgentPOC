import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = { title: "Voice Clinic | Local POC", description: "Synthetic patient voice receptionist demonstration" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en-GB"><body><Providers>{children}</Providers></body></html>;
}
