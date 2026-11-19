"use client";
import { DatabaseView } from "@/components/admin/DatabaseView";
import { WithSnapshot } from "@/components/admin/Telemetry";
import { useUrlState } from "@/components/admin/use-url-state";

export default function DatabasePage() {
  const { params, update } = useUrlState();
  return <WithSnapshot>{data => <DatabaseView data={data} params={params} update={update} />}</WithSnapshot>;
}
