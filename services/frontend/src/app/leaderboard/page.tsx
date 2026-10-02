import { Suspense } from "react";
import type { Metadata } from "next";
import Link from "next/link";
import { Leaderboard } from "@/components/leaderboard/Leaderboard";
import { LoadingSurface } from "@/components/LoadingSurface";
import "./leaderboard.css";

export const metadata: Metadata = { title: "Leaderboard | REVEAL Mechanisms", description: "Explore the public contributions of researchers and the biomedical datasets that inform their scientific accounts." };

export default function LeaderboardPage() {
  return <main id="main" className="leaderboard-page">
    <nav className="page-topbar" aria-label="Leaderboard navigation"><Link className="knowledge-gaps-back" href="/"><span aria-hidden="true">←</span> Knowledge gaps</Link></nav>
    <header className="leaderboard-intro"><h1>Leaderboard</h1></header>
    <Suspense fallback={<LoadingSurface compact title="Loading rankings" skeleton="none" />}><Leaderboard /></Suspense>
  </main>;
}
