import { LoadingSurface } from "@/components/LoadingSurface";
export default function Loading() {
  return <main id="main" className="reading-page"><LoadingSurface title="Opening the exploration" description="Retrieving the question, findings, and evidence gaps." skeleton="record" /></main>;
}
