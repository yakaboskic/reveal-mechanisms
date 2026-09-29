import "server-only";
import { getServerSession } from "next-auth";
import { authOptions } from "./auth";
import { adminBypass, allowedAdmin } from "./admin-policy";
export async function adminAccess() {
  if (adminBypass(process.env)) return { allowed: true, bypass: true, signedIn: false };
  const session = await getServerSession(authOptions);
  return { allowed: allowedAdmin(session?.adminIdentity?.email, session?.adminIdentity?.verified, process.env.ADMIN_EMAILS), bypass: false, signedIn: !!session };
}
