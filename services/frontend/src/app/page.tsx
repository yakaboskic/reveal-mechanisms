import { Composer } from "@/components/Composer";
import { selectionKey } from "@/lib/composer-navigation";
export default async function Home({ searchParams }: { searchParams: Promise<{ job?: string | string[]; draft?: string | string[]; gap?: string | string[] }> }) {
  const params = await searchParams;
  const selected = { job: typeof params.job === "string" ? params.job : null,
    draft: typeof params.draft === "string" ? params.draft : null, gap: typeof params.gap === "string" ? params.gap : null };
  return <Composer key={selectionKey(selected)} initialJobId={selected.job || undefined} initialDraftId={selected.draft || undefined} />;
}
