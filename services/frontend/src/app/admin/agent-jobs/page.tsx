"use client";
import { AdminAgentJobsView } from "@/components/AdminAgentJobs";
import { WithSnapshot } from "@/components/admin/Telemetry";
import { Empty, PageHeading } from "@/components/admin/ui";
import { useUrlState } from "@/components/admin/use-url-state";

export default function AgentJobsPage() {
  const { params, update } = useUrlState();
  return <WithSnapshot>{data => data.agent_jobs
    ? <AdminAgentJobsView agents={data.agent_jobs} kinds={data.job_kinds} params={params} update={update} />
    : <><PageHeading title="Agent jobs" /><Empty>This backend does not report agent job types yet.</Empty></>}</WithSnapshot>;
}
