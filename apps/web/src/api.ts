import { z } from "zod";
export class ApiError extends Error {
  code: string;
  requestId?: string;
  retryAt?: number;
  constructor(
    message: string,
    code: string,
    requestId?: string,
    retryAt?: number,
  ) {
    super(message);
    this.code = code;
    this.requestId = requestId;
    this.retryAt = retryAt;
  }
}
export async function api<T extends z.ZodTypeAny>(
  path: string,
  schema: T,
  init?: RequestInit,
): Promise<z.infer<T>> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError("无法连接服务，请检查网络后重试", "network_error");
  }
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new ApiError(
      `服务返回无效数据（HTTP ${response.status}）`,
      "server_error",
    );
  }
  if (!response.ok) {
    const parsed = z
      .object({
        error: z.object({
          message: z.string(),
          code: z.string(),
          details: z
            .object({
              retry_at: z.number().finite().min(0).max(253402300799).optional(),
            })
            .optional(),
        }),
        request_id: z.string(),
      })
      .safeParse(raw);
    throw parsed.success
      ? new ApiError(
          parsed.data.error.message,
          parsed.data.error.code,
          parsed.data.request_id,
          parsed.data.error.details?.retry_at,
        )
      : new ApiError(`服务返回 HTTP ${response.status}`, "server_error");
  }
  const result = z
    .object({ data: schema, request_id: z.string() })
    .safeParse(raw);
  if (!result.success)
    throw new ApiError("服务数据格式不匹配，请刷新重试", "contract_error");
  return result.data.data;
}
export function artifactUrl(
  taskId: string,
  artifactId: string,
  download = false,
) {
  return `/api/v1/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}${download ? "?download=true" : ""}`;
}
export async function readArtifact(tid: string, aid: string) {
  const r = await fetch(artifactUrl(tid, aid), { credentials: "same-origin" });
  if (!r.ok) throw new Error("产物不可用或已过期");
  return r.text();
}
