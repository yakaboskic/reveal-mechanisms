"use client";
import { WithSnapshot } from "@/components/admin/Telemetry";
import { OverviewView } from "@/components/admin/OverviewView";

export default function OverviewPage() {
  return <WithSnapshot>{data => <OverviewView data={data} />}</WithSnapshot>;
}
