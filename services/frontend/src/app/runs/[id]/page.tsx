import { Composer } from "@/components/Composer";

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Composer key={`run:${id}`} initialJobId={id} />;
}
