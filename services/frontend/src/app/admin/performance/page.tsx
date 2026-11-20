"use client";
import { AdminPerformance } from "@/components/AdminPerformance";
import { WithSnapshot } from "@/components/admin/Telemetry";

export default function PerformancePage() {
  return <WithSnapshot>{data => <AdminPerformance runtime={data.runtime} />}</WithSnapshot>;
}
