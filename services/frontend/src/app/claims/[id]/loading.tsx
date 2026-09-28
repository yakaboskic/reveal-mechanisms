import Link from "next/link";
import { LoadingSurface } from "@/components/LoadingSurface";
export default function LoadingClaimPage() {
  return <main id="main" className="reading-page"><nav className="page-topbar" aria-label="Claim navigation"><Link href="/workspace?tab=accounts">Your scientific accounts</Link><Link href="/">Explore knowledge gaps</Link></nav><h1>Scientific claim</h1><LoadingSurface title="Opening scientific claim" description="Loading the assessment, supporting evidence and source provenance." skeleton="record" /></main>;
}
