"use client";
import { ActivityView } from "@/components/admin/ActivityView";
import { WithSnapshot } from "@/components/admin/Telemetry";
import { useUrlState } from "@/components/admin/use-url-state";

export default function ActivityPage() {
  const { params, update } = useUrlState();
  return <WithSnapshot>{data => <ActivityView data={data} params={params} update={update} />}</WithSnapshot>;
}
