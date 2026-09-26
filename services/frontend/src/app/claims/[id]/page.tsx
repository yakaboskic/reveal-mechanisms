import { ClaimView } from "@/components/Scientific";
export default async function ClaimPage({ params }: { params: Promise<{ id: string }> }) { const { id } = await params; return <ClaimView id={decodeURIComponent(id)} />; }
