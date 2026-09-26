import { AccountView } from "@/components/Scientific";
export default async function AccountPage({ params }: { params: Promise<{ id: string }> }) { const { id } = await params; return <main id="main" className="reading-page"><AccountView id={decodeURIComponent(id)} /></main>; }
