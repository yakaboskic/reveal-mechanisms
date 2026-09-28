import Link from "next/link";
import { LoadingSurface } from "@/components/LoadingSurface";
export default function LoadingRecordPage() {
  return <main id="main" className="reading-page"><nav className="page-topbar" aria-label="Scientific record navigation"><Link href="/">Explore knowledge gaps</Link><Link href="/workspace?tab=accounts">Your scientific accounts</Link></nav><h1>Scientific record</h1><LoadingSurface title="Opening scientific record" description="Retrieving its saved content, identity and source provenance." skeleton="record" /></main>;
}
