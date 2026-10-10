import { TraitView } from "@/components/TraitView";
import { notFound } from "next/navigation";
export const metadata = { title: "Trait | REVEAL" };
export default async function TraitPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ from?: string }> }) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  let exact: string;
  try { exact = decodeURIComponent(id); } catch { notFound(); }
  if (!/^KPN\.TRAIT:\d{7}$/.test(exact)) notFound();
  return <TraitView id={exact} from={query.from} />;
}
