import type { Metadata } from "next";
import { Session } from "@/components/Session";
import "./globals.css";
import "@/components/composer.css";
export const metadata: Metadata = { title: "REVEAL Mechanisms", description: "Explore knowledge gaps through linked mechanisms, scientific accounts, and their evidence." };
export default function Layout({ children }: { children: React.ReactNode }) { return <html lang="en"><body><Session>{children}</Session></body></html>; }
