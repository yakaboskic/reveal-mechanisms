import { Composer } from "@/components/Composer";
export default async function Home({ searchParams }: { searchParams: Promise<{ job?: string | string[] }> }) {
  const { job } = await searchParams;
  return <Composer initialJobId={typeof job === "string" ? job : undefined} />;
}
