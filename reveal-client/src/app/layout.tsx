import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "REVEAL Close the gap", description: "Choose the gap. Ground the claim." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
