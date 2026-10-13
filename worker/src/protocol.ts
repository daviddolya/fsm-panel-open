import { WorkerError, VERSION } from "./types.ts";
import type { Request } from "./types.ts";

const ID = /^[a-zA-Z0-9_.:-]{1,128}$/;
const DECIMAL = /^[1-9][0-9]{0,19}$/;
const methods = new Set(["inventory", "send", "confirm", "reconcile", "status", "cancel"]);
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function requireValid(value: unknown): asserts value {
  if (!value) throw new WorkerError("invalid_request");
}
function text(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max &&
    !/[\x00-\x1f]/.test(value);
}
export function parseRequest(value: unknown): Request {
  requireValid(record(value));
  if (value.version !== VERSION) throw new WorkerError("protocol_version");
  requireValid(Object.keys(value).every(k =>
    ["version", "request_id", "method", "params", "timeout_ms", "admission"].includes(k)));
  requireValid(typeof value.request_id === "string" && ID.test(value.request_id));
  requireValid(typeof value.method === "string" && methods.has(value.method));
  requireValid(Number.isSafeInteger(value.timeout_ms) &&
    Number(value.timeout_ms) >= 10 && Number(value.timeout_ms) <= 300_000);
  requireValid(record(value.params));
  const p = value.params;
  const allowed = ["account_id", "operation_id", "trade_url", "inventories", "credentials",
    "allow_partial", "target_request_id"];
  requireValid(Object.keys(p).every(k => allowed.includes(k)));
  if (value.admission !== undefined) {
    requireValid(record(value.admission) && Object.keys(value.admission).length === 1 &&
      Number.isSafeInteger(value.admission.expires_at_ms));
  }
  if (value.method === "cancel") {
    requireValid(typeof p.target_request_id === "string" && ID.test(p.target_request_id));
  } else {
    requireValid(typeof p.account_id === "string" && ID.test(p.account_id));
    if (value.method !== "inventory") {
      requireValid(typeof p.operation_id === "string" && ID.test(p.operation_id));
    }
    if (["inventory", "send", "confirm", "reconcile"].includes(value.method)) {
      requireValid(record(p.credentials));
      requireValid(Object.keys(p.credentials).every(k =>
        ["account_name", "password", "shared_secret", "identity_secret"].includes(k)));
      requireValid(text(p.credentials.account_name, 128) && text(p.credentials.password, 1024) &&
        text(p.credentials.shared_secret, 1024));
      if (p.credentials.identity_secret !== undefined) {
        requireValid(text(p.credentials.identity_secret, 1024));
      }
    }
    if (["inventory", "send"].includes(value.method)) {
      if (p.inventories === undefined) p.inventories = [{ app_id: 730, context_id: "2" }];
      requireValid(Array.isArray(p.inventories) && p.inventories.length > 0 &&
        p.inventories.length <= 20);
      for (const item of p.inventories) {
        requireValid(record(item) && Object.keys(item).length === 2 &&
          Number.isSafeInteger(item.app_id) && Number(item.app_id) > 0 &&
          Number(item.app_id) <= 2147483647 && typeof item.context_id === "string" &&
          DECIMAL.test(item.context_id));
      }
    }
    if (value.method === "send") {
      requireValid(text(p.trade_url, 2048));
      try {
        const url = new URL(p.trade_url);
        requireValid(url.protocol === "https:" && url.hostname === "steamcommunity.com" &&
          !url.port && !url.username && !url.password && !url.hash &&
          url.pathname === "/tradeoffer/new/" &&
          DECIMAL.test(url.searchParams.get("partner") ?? "") &&
          [...url.searchParams.keys()].every(k => ["partner", "token"].includes(k)) &&
          url.searchParams.getAll("partner").length === 1 &&
          url.searchParams.getAll("token").length <= 1);
      } catch { throw new WorkerError("invalid_request"); }
      requireValid(p.allow_partial === undefined || typeof p.allow_partial === "boolean");
    }
  }
  return value as unknown as Request;
}
