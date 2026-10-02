import { notFound } from "next/navigation";
import { GapDetail } from "@/components/GapDetail";
import { gapPageId } from "@/components/gap-reading";

export default async function KnowledgeGapPage({ params }: { params: Promise<{ gapId: string }> }) {
  const { gapId } = await params;
  const id = gapPageId(gapId);
  if (!id) notFound();
  return <main id="main" className="gap-detail-page"><GapDetail key={id} id={id} /></main>;
}
