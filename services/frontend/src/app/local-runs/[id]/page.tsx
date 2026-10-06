import { LocalResearchPage } from "@/components/LocalResearchPage";
export default async function LocalRunPage({ params }: { params: Promise<{ id: string }> }) {
  return <LocalResearchPage id={(await params).id} />;
}
