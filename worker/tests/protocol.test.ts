import { expect, it } from "vitest";
import { parseRequest } from "../src/protocol.ts";

it("rejects unsupported protocol, unbounded timeouts, injection fields and unsafe URLs", () => {
  const input = { version: 1, request_id: "request-a", method: "send", timeout_ms: 1000,
    params: { account_id: "account-a", operation_id: "operation-a",
      credentials: { account_name: "synthetic", password: "private", shared_secret: "private" },
      trade_url: "https://steamcommunity.com/tradeoffer/new/?partner=123" } };
  expect(parseRequest(structuredClone(input)).params.inventories).toEqual([{ app_id: 730, context_id: "2" }]);
  for (const patch of [{ version: 2 }, { timeout_ms: 300001 }, { timeout_ms: true },
    { request_id: "bad\nrequest" }, { execute: "code" }]) {
    expect(() => parseRequest({ ...structuredClone(input), ...patch })).toThrow();
  }
  for (const trade_url of ["http://steamcommunity.com/tradeoffer/new/?partner=123",
    "https://example.org/tradeoffer/new/?partner=123", "https://steamcommunity.com@evil.org/",
    "https://steamcommunity.com/tradeoffer/new/?partner=123&partner=456"]) {
    expect(() => parseRequest({ ...input, params: { ...input.params, trade_url } })).toThrow();
  }
});
