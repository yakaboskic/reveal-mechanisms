import { redirect } from "next/navigation";
export default function LocalRunsPage() {
  redirect("/workspace?tab=runs");
}
