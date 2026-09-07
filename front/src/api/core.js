import { translateSaved } from "../i18n/runtime";
import { API_BASE_URL } from "../runtime-config";
import * as platform from "../utils/platform";
/* eslint-disable import/extensions */
// eslint-disable-next-line import/extensions
import { MOCK_SILENT_AUDIO_URL } from "./mock/media";
import { mockBlobRequest, mockRequest } from "./mock/request";

// Общая транспортная часть локального REST API.
export const MOCK_API_ENABLED = import.meta.env.VITE_USE_MOCK_API === "true";
export const BASE_URL = API_BASE_URL;

export const isAmbiguousTransportError = (error) =>
  error?.name === "TimeoutError" ||
  error?.name === "AbortError" ||
  (error instanceof TypeError && !error.status);

const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
function createDeadlineSignal(signal, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
  if (typeof globalThis.AbortController !== "function") {
    return { signal, cleanup: () => {}, timedOut: () => false };
  }
  const controller = new globalThis.AbortController();
  let didTimeout = false;
  const abort = () => controller.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener?.("abort", abort, { once: true });
  const safeTimeout = Number.isFinite(Number(timeoutMs))
    ? Math.max(1, Number(timeoutMs))
    : DEFAULT_REQUEST_TIMEOUT_MS;
  const timer = globalThis.setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, safeTimeout);
  return {
    signal: controller.signal,
    timedOut: () => didTimeout,
    cleanup: () => {
      globalThis.clearTimeout(timer);
      signal?.removeEventListener?.("abort", abort);
    }
  };
}
function normalizeHeaders(headers) {
  if (!headers) return undefined;
  if (Array.isArray(headers)) return Object.fromEntries(headers);
  if (typeof headers.entries === "function") return Object.fromEntries(headers.entries());
  return { ...headers };
}
function hasContentType(headers) {
  return Object.keys(headers).some((name) => name.toLowerCase() === "content-type");
}
function buildRequestOptions(options = {}) {
  const { headers, body, timeoutMs: _, ...requestOptions } = options;
  const FormDataCtor = globalThis.FormData;
  const isFormData = typeof FormDataCtor === "function" && body instanceof FormDataCtor;
  const nextHeaders = normalizeHeaders(headers) || {};
  const apiToken = platform.apiToken();
  if (apiToken) nextHeaders["X-ADVoice-Token"] = apiToken;
  if (isFormData || body == null) {
    return {
      ...requestOptions,
      body,
      ...(Object.keys(nextHeaders).length ? { headers: nextHeaders } : {})
    };
  }
  if (typeof body === "string" && !hasContentType(nextHeaders)) {
    nextHeaders["Content-Type"] = "application/json";
  }
  return { ...requestOptions, body, headers: nextHeaders };
}
async function readErrorDetail(response) {
  let detail = response.statusText || `HTTP ${response.status}`;
  try {
    const data = await response.json();
    detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    if (detail.startsWith("errors.")) detail = translateSaved(detail);
  } catch {
    // Ответ может не содержать JSON-тело.
  }
  return detail;
}
async function withSuccessfulResponse(path, options, consume) {
  if (typeof globalThis.fetch !== "function")
    throw new Error(translateSaved("api.fetchApiIsNotAvailableInTheCurrentEnvironment"));
  const normalizedPath = String(path || "");
  const requestPath = normalizedPath.startsWith("/") ? normalizedPath : `/${normalizedPath}`;
  const deadline = createDeadlineSignal(options?.signal, options?.timeoutMs);
  try {
    const response = await globalThis.fetch(`${BASE_URL}${requestPath}`, {
      ...buildRequestOptions(options),
      signal: deadline.signal
    });
    if (!response.ok) {
      const error = new Error(await readErrorDetail(response));
      error.status = response.status;
      error.url = response.url || `${BASE_URL}${requestPath}`;
      throw error;
    }
    return await consume(response);
  } catch (error) {
    if (!deadline.timedOut()) throw error;
    const timeoutError = new Error(translateSaved("api.backendResponseTimedOut"));
    timeoutError.name = "TimeoutError";
    throw timeoutError;
  } finally {
    deadline.cleanup();
  }
}

const MAX_MEMORY_BLOB_BYTES = 64 * 1024 * 1024;
async function readBlobResponse(response) {
  const getDirectory = globalThis.navigator?.storage?.getDirectory;
  if (typeof getDirectory === "function" && response.body?.getReader) {
    const root = await getDirectory.call(globalThis.navigator.storage);
    const name = `advoice-http-${Date.now()}-${Math.random().toString(16).slice(2)}.part`;
    const handle = await root.getFileHandle(name, { create: true });
    const writable = await handle.createWritable();
    const reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        await writable.write(value);
      }
      await writable.close();
      const file = await handle.getFile();
      Object.defineProperty(file, "cleanup", {
        value: () => root.removeEntry(name).catch(() => {})
      });
      return file;
    } catch (error) {
      await writable.abort?.().catch?.(() => {});
      await root.removeEntry(name).catch(() => {});
      throw error;
    }
  }
  if (response.body?.getReader) {
    const chunks = [];
    let size = 0;
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_MEMORY_BLOB_BYTES) {
        await reader.cancel().catch(() => {});
        throw new Error(translateSaved("api.fileIsTooLargeToUploadWithoutBrowserDisk"));
      }
      chunks.push(value);
    }
    return new Blob(chunks, {
      type: response.headers?.get?.("content-type") || "application/octet-stream"
    });
  }
  return response.blob();
}

export function encodePathSegment(value) {
  const segment = String(value ?? "").trim();
  if (!segment) throw new TypeError(translateSaved("api.emptyApiResourceId"));
  return encodeURIComponent(segment);
}
export async function request(path, options = {}) {
  if (MOCK_API_ENABLED) return mockRequest(path, options);
  return withSuccessfulResponse(path, options, async (response) => {
    if (response.status === 204) return null;
    const text = await response.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(translateSaved("api.incorrectJsonInResponse", { 0: response.url || path }));
    }
  });
}
export async function requestBlob(path, options = {}) {
  if (MOCK_API_ENABLED) return mockBlobRequest(path, options);
  return withSuccessfulResponse(path, options, readBlobResponse);
}
export function createFileUrl(path) {
  if (MOCK_API_ENABLED) return MOCK_SILENT_AUDIO_URL;
  const normalizedPath = String(path || "").trim();
  if (!normalizedPath) return BASE_URL;
  if (/^[a-z][a-z\d+.-]*:/i.test(normalizedPath)) {
    throw new TypeError(translateSaved("api.expectedLocalPathToApiFile"));
  }
  const requestPath = normalizedPath.startsWith("/") ? normalizedPath : `/${normalizedPath}`;
  return `${BASE_URL}${requestPath}`;
}
