"use client";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { urlWith, type UrlValue } from "@/lib/admin-url";

export type UrlUpdate = (updates: Record<string, UrlValue>, options?: { push?: boolean }) => void;

/** View state lives in the URL, as in the claims console: deliberate changes push history, typing replaces it. */
export function useUrlState() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const update = useCallback<UrlUpdate>((updates, { push = true } = {}) => {
    const url = urlWith(pathname, params, updates);
    if (push) router.push(url, { scroll: false }); else router.replace(url, { scroll: false });
  }, [params, router, pathname]);
  return { params, pathname, update };
}
