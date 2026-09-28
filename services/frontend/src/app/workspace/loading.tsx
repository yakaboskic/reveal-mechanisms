import Link from "next/link";
import { LoadingSurface } from "@/components/LoadingSurface";
import "@/components/workspace-activity.css";
export default function LoadingWorkspacePage() {
  return <main id="main" className="reading-page workspace-dashboard"><nav className="workspace-topbar" aria-label="Workspace navigation"><Link href="/">← Explore knowledge gaps</Link></nav><div className="workspace-heading"><h1>Your workspace</h1></div><LoadingSurface title="Opening your workspace" description="Retrieving your saved knowledge gaps and scientific accounts." /></main>;
}
