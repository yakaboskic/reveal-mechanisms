import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "REVEAL client", description: "A standalone client for the REVEAL research API." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
