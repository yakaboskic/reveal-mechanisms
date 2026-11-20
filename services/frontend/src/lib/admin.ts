import "server-only";
import { getServerSession } from "next-auth";
import { authOptions } from "./auth";
import { adminBypass, adminEmail } from "./admin-policy";
/** email is the allowed admin's verified email (lower-cased); null on the development bypass and when access is denied. */
export async function adminAccess(): Promise<{ allowed: boolean; bypass: boolean; signedIn: boolean; email: string | null }> {
  if (adminBypass(process.env)) return { allowed: true, bypass: true, signedIn: false, email: null };
  const session = await getServerSession(authOptions);
  const email = adminEmail(session?.adminIdentity?.email, session?.adminIdentity?.verified, process.env.ADMIN_EMAILS);
  return { allowed: email !== null, bypass: false, signedIn: !!session, email };
}
