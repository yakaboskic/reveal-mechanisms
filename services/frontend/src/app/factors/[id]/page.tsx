import { FactorView } from "@/components/FactorView";
import { notFound } from "next/navigation";
export const metadata = { title: "Factor loadings | REVEAL" };
export default async function FactorPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ source_revision?: string; archive?: string; from?: string }> }) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  let exact: string;
  try { exact = decodeURIComponent(id); } catch { notFound(); }
  return <FactorView sourceId={exact} revision={query.source_revision} archiveId={query.archive} from={query.from} />;
}
