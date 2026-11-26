// LSX-VE management API client. All mutating requests carry the session CSRF token.

export class ApiError extends Error {
  status: number;
  code: string;
  field?: string;
  constructor(status: number, code: string, message: string, field?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

let csrfToken = "";
let onUnauthorized: (() => void) | null = null;

export function setCsrf(t: string) {
  csrfToken = t;
}
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let resp: Response;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the LSX-VE host");
  }
  const text = await resp.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!resp.ok) {
    const e = data?.error ?? {};
    if (resp.status === 401 && onUnauthorized && !path.endsWith("/auth/login")) onUnauthorized();
    throw new ApiError(resp.status, e.code ?? "error", e.message ?? `HTTP ${resp.status}`, e.details?.field);
  }
  return data as T;
}

export const api = {
  get: <T = any>(p: string) => request<T>("GET", p),
  post: <T = any>(p: string, b: unknown = {}) => request<T>("POST", p, b),
  put: <T = any>(p: string, b: unknown = {}) => request<T>("PUT", p, b),
  patch: <T = any>(p: string, b: unknown = {}) => request<T>("PATCH", p, b),
  del: <T = any>(p: string) => request<T>("DELETE", p),
};

/** Streams a file to the ISO library with upload progress (XHR gives progress events). */
export function uploadIso(file: File, onProgress: (loaded: number, total: number) => void): Promise<any> {
  return uploadFile(`/api/v1/storage/isos/${encodeURIComponent(file.name)}`, file, onProgress);
}

/** Streams a file (PUT, raw body) with upload progress. */
export function uploadFile(url: string, file: File, onProgress: (loaded: number, total: number) => void): Promise<any> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("X-CSRF-Token", csrfToken);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => onProgress(e.loaded, e.total);
    xhr.onload = () => {
      let data: any = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, data?.error?.code ?? "error", data?.error?.message ?? `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new ApiError(0, "network", "Upload interrupted"));
    xhr.send(file);
  });
}

export const enc = encodeURIComponent;
