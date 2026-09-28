import { AnalysisOutcomeView } from "@/components/AnalysisOutcome";
export default async function AnalysisPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <main id="main" className="reading-page"><AnalysisOutcomeView id={decodeURIComponent(id)} /></main>;
}
