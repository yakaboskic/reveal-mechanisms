import { Composer } from "@/components/Composer";
export default async function Home({ searchParams }: { searchParams: Promise<{ job?: string | string[]; draft?: string | string[] }> }) {
  const { job, draft } = await searchParams;
  return <Composer initialJobId={typeof job === "string" ? job : undefined} initialDraftId={typeof draft === "string" ? draft : undefined} />;
}
