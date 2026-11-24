"use client";
import { AdminCostsView } from "@/components/AdminCosts";
import { WithSnapshot } from "@/components/admin/Telemetry";
import { Empty, PageHeading } from "@/components/admin/ui";

export default function CostsPage() {
  return <WithSnapshot>{data => data.costs
    ? <AdminCostsView costs={data.costs} jobs={data.jobs} owners={data.owners} />
    : <><PageHeading title="Costs" /><Empty>This backend does not report agent costs yet.</Empty></>}</WithSnapshot>;
}
