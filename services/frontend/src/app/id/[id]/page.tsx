import { redirect } from "next/navigation";
import { ObjectView } from "@/components/ObjectView";
import { traitPageHref } from "@/lib/factor-links";
export default async function Resolver({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const exact = decodeURIComponent(id);
  if (exact.startsWith("dapper:Claim.")) redirect(`/claims/${encodeURIComponent(exact)}`);
  if (exact.startsWith("dapper:ScientificAccount.")) redirect(`/accounts/${encodeURIComponent(exact)}`);
  if (exact.startsWith("dapper:GeneSet.")) redirect(`/gene-sets/${encodeURIComponent(exact)}`);
  // Only an exact KPN trait id has a trait page; anything else stays the object record, as before.
  const trait = traitPageHref(exact);
  if (trait) redirect(trait);
  return <ObjectView id={exact} />;
}
