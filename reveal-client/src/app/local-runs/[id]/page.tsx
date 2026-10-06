import { LocalWorkView } from "@/components/LocalWork";
export default async function LocalRunPage({ params }: { params: Promise<{ id: string }> }) {
  return <LocalWorkView key={(await params).id} id={(await params).id} standalone />;
}
