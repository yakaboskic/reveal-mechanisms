"use client";
import { useTelemetry, WithSnapshot } from "@/components/admin/Telemetry";
import { JobsView } from "@/components/admin/JobsView";
import { useUrlState } from "@/components/admin/use-url-state";

export default function JobsPage() {
  const { params, update } = useUrlState();
  const { auto } = useTelemetry();
  return <WithSnapshot>{data => <JobsView data={data} params={params} update={update} auto={auto} />}</WithSnapshot>;
}
