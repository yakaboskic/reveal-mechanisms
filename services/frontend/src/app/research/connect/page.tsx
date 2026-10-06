import { Suspense } from "react";
import { ResearchConsentPage } from "@/components/ResearchConsent";
export const metadata = { title: "Connect your agent · Reveal", robots: { index: false, follow: false }, referrer: "no-referrer" };
export default function Page() {
  return <Suspense fallback={<main id="main" className="local-work-page"><p role="status">Opening connection request…</p></main>}><ResearchConsentPage /></Suspense>;
}
