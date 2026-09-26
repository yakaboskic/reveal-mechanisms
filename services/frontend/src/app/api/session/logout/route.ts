import { cookies } from "next/headers";
import { anonymousCookie, checkOrigin, gatewayProblem } from "@/lib/gateway";
export async function POST(request: Request) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  (await cookies()).delete(anonymousCookie);
  return Response.json({ ok: true });
}
