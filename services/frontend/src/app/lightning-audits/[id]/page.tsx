import { LightningAuditPage } from "@/components/LightningAudit";

export default async function AuditPage({ params }: { params: Promise<{ id: string }> }) {
  return <LightningAuditPage id={(await params).id} />;
}
