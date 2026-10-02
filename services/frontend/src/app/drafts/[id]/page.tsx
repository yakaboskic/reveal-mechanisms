import { Composer } from "@/components/Composer";

export default async function DraftPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Composer key={`draft:${id}`} initialDraftId={id} />;
}
