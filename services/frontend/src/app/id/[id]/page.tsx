import { redirect } from "next/navigation";
import { ObjectView } from "@/components/ObjectView";
export default async function Resolver({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const exact = decodeURIComponent(id);
  if (exact.startsWith("dapper:Claim.")) redirect(`/claims/${encodeURIComponent(exact)}`);
  if (exact.startsWith("dapper:ScientificAccount.")) redirect(`/accounts/${encodeURIComponent(exact)}`);
  if (exact.startsWith("dapper:GeneSet.")) redirect(`/gene-sets/${encodeURIComponent(exact)}`);
  return <ObjectView id={exact} />;
}
