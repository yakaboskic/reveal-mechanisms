import { JobDetailPage } from "@/components/admin/JobDetail";

// One job's detail. A server component so the Box console template is read from the server's environment at request
// time (it is not a NEXT_PUBLIC_ variable); unset, the page shows the Box id to copy instead of a link.
export default async function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <JobDetailPage key={id} jobId={id} boxConsole={process.env.REVEAL_BOX_CONSOLE_URL_TEMPLATE || null} />;
}
