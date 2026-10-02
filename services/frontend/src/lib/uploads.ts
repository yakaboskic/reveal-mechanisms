import { ApiError, type Schema } from "./client";
import { withRequestDeadline } from "./request-deadline";

export type Upload = Schema<"Upload">;
export type UploadTicket = { upload: Upload; transfer: { method: "POST"; url: string; fields: Record<string, string>; encoding: "multipart" | "base64" } };

export async function uploadRequest<T>(path: string, method = "GET", body?: unknown, key?: string, signal?: AbortSignal): Promise<T> {
  return withRequestDeadline(async deadline => {
    const response = await fetch(`/api/backend${path}`, { method, credentials: "same-origin", signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
      headers: { "content-type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const result = await response.json();
    if (!response.ok) throw new ApiError(response.status, result.code || "UPLOAD_FAILED", result.detail || "The document could not be attached.");
    return result as T;
  });
}

export async function fileDigest(file: File) {
  const bytes = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, "0")).join("");
}

/** The signed destination is returned only by the authenticated upload service. */
export async function transferFile(ticket: UploadTicket, file: File, progress: (percent: number) => void, signal: AbortSignal) {
  let body: FormData | string;
  let destination = ticket.transfer.url;
  if (ticket.transfer.encoding === "base64") {
    if (!destination.startsWith("/v1/uploads/")) throw new Error("Invalid local upload destination.");
    destination = `/api/backend${destination}`;
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    body = JSON.stringify({ content_base64: btoa(binary) });
  } else {
    const url = new URL(destination);
    if (url.protocol !== "https:" && url.hostname !== "localhost" && url.hostname !== "127.0.0.1") throw new Error("An encrypted upload destination is required.");
    const form = new FormData();
    for (const [key, value] of Object.entries(ticket.transfer.fields)) form.append(key, value);
    form.append("file", file); body = form;
  }
  if (signal.aborted) throw new DOMException("Upload cancelled", "AbortError");
  await new Promise<void>((resolve, reject) => {
    const request = new XMLHttpRequest();
    const abort = () => request.abort();
    const cleanup = () => signal.removeEventListener("abort", abort);
    request.open("POST", destination); request.timeout = 120_000;
    if (typeof body === "string") request.setRequestHeader("content-type", "application/json");
    request.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
    request.onload = () => { cleanup(); if (request.status >= 200 && request.status < 300) resolve(); else reject(new Error("Document transfer failed. Retry the upload.")); };
    request.onerror = () => { cleanup(); reject(new Error("Could not reach document storage. Check your connection and retry.")); };
    request.ontimeout = () => { cleanup(); reject(new Error("Document transfer timed out. Retry the upload.")); };
    request.onabort = () => { cleanup(); reject(new DOMException("Upload cancelled", "AbortError")); };
    signal.addEventListener("abort", abort, { once: true }); request.send(body);
  });
}
