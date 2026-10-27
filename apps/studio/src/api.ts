import type { Capabilities, Job, Options, Voice } from "./types";

export const desktop = "__TAURI_INTERNALS__" in window;
let accessKey = "";
export function setAccessKey(value: string) { accessKey = value; }

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); }
}

export async function native<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

const pendingKeys = new Map<string, string>();

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let operation = "";
  if (["POST", "PUT", "PATCH", "DELETE"].includes(init.method || "") && !path.startsWith("/launcher/")) {
    let content = typeof init.body === "string" ? init.body : "";
    if (init.body instanceof FormData) {
      content = JSON.stringify(await Promise.all(Array.from(init.body.entries()).map(async ([key, value]) => [key, typeof value === "string" ? value : Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await value.arrayBuffer())))])));
    }
    operation = `${init.method}:${path}:${content}`;
    const headers = new Headers(init.headers);
    if (!headers.has("Idempotency-Key")) {
      if (pendingKeys.size > 32) pendingKeys.delete(pendingKeys.keys().next().value!);
      const key = pendingKeys.get(operation) || crypto.randomUUID();
      pendingKeys.set(operation, key); headers.set("Idempotency-Key", key);
    }
    init = { ...init, headers };
  }
  let response: Response;
  if (desktop) {
    const result = await native<{ status: number; body: string }>("api_request", {
      idempotencyKey: new Headers(init.headers).get("Idempotency-Key"),
      path, method: init.method || "GET", body: typeof init.body === "string" ? init.body : null,
    });
    response = new Response(result.body, { status: result.status });
  } else {
    const headers = new Headers(init.headers);
    if (accessKey) headers.set("Authorization", `Bearer ${accessKey}`);
    if (typeof init.body === "string") headers.set("Content-Type", "application/json");
    response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  }
  if (!response.ok) {
    if (operation && response.status >= 400 && response.status < 500 && response.status !== 429) pendingKeys.delete(operation);
    const error = await response.json().catch(() => ({}));
    const detail = Array.isArray(error.detail) ? error.detail.map((v: { msg: string }) => v.msg).join("; ") : error.detail;
    throw new ApiError(error.code || "request_failed", error.message || detail || `Request failed (${response.status})`, response.status);
  }
  if (operation) pendingKeys.delete(operation);
  return response.json();
}

export async function importVoice(file: File, name: string, start: number, end: number): Promise<Voice> {
  if (desktop) {
    const buffer = await file.arrayBuffer();
    const signature = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", buffer))).join(",");
    const operation = `import:${name}:${start}:${end}:${signature}`;
    const idempotencyKey = pendingKeys.get(operation) || crypto.randomUUID();
    pendingKeys.set(operation, idempotencyKey);
    const bytes = Array.from(new Uint8Array(buffer));
    const result = await native<{ status: number; body: string }>("import_voice", { bytes, name, start, end, authorized: true, idempotencyKey });
    if (result.status >= 400) { const error = JSON.parse(result.body); throw new Error(error.message || "Import failed"); }
    pendingKeys.delete(operation);
    return JSON.parse(result.body);
  }
  const form = new FormData();
  form.set("file", file); form.set("name", name); form.set("start", String(start));
  form.set("end", String(end)); form.set("authorized", "true");
  return request("/v1/voices", { method: "POST", body: form });
}

export async function artifactBlob(path: string): Promise<Blob> {
  if (desktop) {
    const data = await native<{ bytes: number[]; content_type: string }>("artifact_bytes", { path });
    return new Blob([new Uint8Array(data.bytes)], { type: data.content_type });
  }
  const response = await fetch(path, { headers: accessKey ? { Authorization: `Bearer ${accessKey}` } : {}, credentials: "same-origin" });
  if (!response.ok) throw new Error("Audio is unavailable or expired.");
  return response.blob();
}

export async function saveArtifact(path: string, filename: string) {
  if (desktop) return native("save_artifact", { path, filename });
  const url = URL.createObjectURL(await artifactBlob(path));
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const api = {
  capabilities: () => request<Capabilities>("/v1/capabilities"),
  voices: () => request<Voice[]>("/v1/voices"),
  jobs: () => request<Job[]>("/v1/generations"),
  generate: (voice: string, text: string, options: Options, purpose = "message") =>
    request<Job>("/v1/generations", {
      method: "POST",
      body: JSON.stringify({ voice_id: voice, text, options, purpose }),
    }),
  cancel: (id: string) => request<Job>(`/v1/generations/${id}/cancel`, { method: "POST" }),
  removeJob: (id: string) => request(`/v1/generations/${id}`, { method: "DELETE" }),
  removeVoice: (id: string) => request(`/v1/voices/${id}`, { method: "DELETE" }),
  activate: (id: string, auditionId: string) => request<Voice>(`/v1/voices/${id}/activate`, {
    method: "POST", body: JSON.stringify({ audition_job_id: auditionId, approved: true }),
  }),
  install: (name: string) => request(`/v1/models/${name}/install`, { method: "POST" }),
};
