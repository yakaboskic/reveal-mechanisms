import { GeneSetView } from "@/components/GeneSetView";
import { notFound } from "next/navigation";
export const metadata = { title: "Gene-set provenance | REVEAL" };
export default async function GeneSetPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ generation_id?: string; from?: string }> }) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  let exact: string;
  try { exact = decodeURIComponent(id); } catch { notFound(); }
  return <GeneSetView id={exact} generation={query.generation_id} from={query.from} />;
}
