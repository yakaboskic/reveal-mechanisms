import { handleSession } from "@/lib/session";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
async function session(request: Request) { return handleSession(request); }
export { session as GET, session as POST, session as DELETE };
