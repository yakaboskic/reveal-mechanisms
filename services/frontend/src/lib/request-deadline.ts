export const requestDeadlineMs = 30_000;

export class RequestTimeoutError extends Error {
  constructor(message = "The server is taking longer than expected. Your selections are saved. Please retry.") {
    super(message);
    this.name = "RequestTimeoutError";
  }
}

// Include response-body decoding in the deadline, not just arrival of headers.
export async function withRequestDeadline<T>(
  request: (signal: AbortSignal) => Promise<T>,
  message?: string,
  timeoutMs = requestDeadlineMs,
): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      const error = new RequestTimeoutError(message);
      reject(error);
      controller.abort(error);
    }, timeoutMs);
  });
  try { return await Promise.race([request(controller.signal), deadline]); }
  finally { clearTimeout(timer!); }
}
