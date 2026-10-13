import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Journal } from "../src/journal.ts";
import { Engine } from "../src/engine.ts";
import { SimulationTransport } from "../src/simulation.ts";
import { parseRequest } from "../src/protocol.ts";
import type { Scenario } from "../src/simulation.ts";
import type { Request, TradeResult } from "../src/types.ts";

const directories: string[] = [];
const journals: Journal[] = [];
const credentials = { account_name: "synthetic", password: "private-password",
  shared_secret: "private-shared", identity_secret: "private-identity" };
const asset = (id: string, app_id = 730) => ({ app_id, context_id: "2", asset_id: id, amount: 1 });
const populated: Scenario = { pages: { "730/2": [{ assets: [asset("1")] }] } };
function fixture(scenario: Scenario = populated) {
  const directory = mkdtempSync(join(tmpdir(), "fsm-worker-"));
  directories.push(directory);
  const path = join(directory, "trades.sqlite");
  const journal = new Journal(path, true);
  journals.push(journal);
  const transport = new SimulationTransport(scenario);
  const engine = new Engine(journal, () => transport);
  return { path, journal, transport, engine };
}
function request(method = "send", params: object = {}, extra: object = {}): Request {
  return parseRequest({ version: 1, request_id: randomUUID(), method, timeout_ms: 1000,
    admission: { expires_at_ms: Date.now() + 30_000 }, params: {
      account_id: "account-a", operation_id: "intent-a", credentials,
      trade_url: "https://steamcommunity.com/tradeoffer/new/?partner=123&token=synthetic",
      inventories: [{ app_id: 730, context_id: "2" }], ...params,
    }, ...extra });
}
afterEach(() => {
  for (const journal of journals.splice(0)) { try { journal.close(); } catch {} }
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
});
describe("durable offer effects", () => {
  it("returns explicit sent and never repeats an operation or duplicate request", async () => {
    const { engine, transport } = fixture();
    const first = request();
    expect((await engine.handle(first)).result).toMatchObject({ state: "sent", simulation: true });
    expect((await engine.handle(first)).error?.code).toBe("duplicate_request");
    expect((await engine.handle(request())).result).toMatchObject({ state: "sent" });
    expect(transport.sendCount).toBe(1);
  });
  it("persists pending then confirms once after admission has expired", async () => {
    const { engine, transport } = fixture({ ...populated, send_status: "pending" });
    expect((await engine.handle(request())).result).toMatchObject({ state: "created_pending_confirmation" });
    const confirm = () => request("confirm", {}, { admission: undefined });
    expect((await engine.handle(confirm())).result).toMatchObject({ state: "confirmed" });
    expect((await engine.handle(confirm())).result).toMatchObject({ state: "confirmed" });
    expect(transport.confirmCount).toBe(1);
    expect(transport.sendCount).toBe(1);
  });
  it("lost send response stays unknown after restart and cannot be resent or inferred absent", async () => {
    const { engine, transport, journal, path } = fixture({ ...populated, send_error: true });
    expect((await engine.handle(request())).result).toMatchObject({ state: "unknown_outcome" });
    journal.close();
    const reopened = new Journal(path, true); journals.push(reopened);
    const other = new SimulationTransport(populated);
    const restarted = new Engine(reopened, () => other);
    expect((await restarted.handle(request())).result).toMatchObject({ state: "unknown_outcome" });
    expect((await restarted.handle(request("reconcile", {}, { admission: undefined }))).result)
      .toMatchObject({ state: "unknown_outcome", offer_id: null });
    expect(other.sendCount).toBe(0);
    expect(transport.sendCount).toBe(1);
  });
  it("crash after journal send boundary recovers unknown without dispatch", async () => {
    const { journal, path } = fixture();
    journal.reserve(request());
    journal.claim("intent-a", "account-a", "not_sent", "sending");
    journal.close();
    const reopened = new Journal(path, true); journals.push(reopened);
    const transport = new SimulationTransport(populated);
    expect((await new Engine(reopened, () => transport).handle(request())).result)
      .toMatchObject({ state: "unknown_outcome", code: "recovered_after_restart" });
    expect(transport.sendCount).toBe(0);
  });
  it("changed destination or account cannot consume an existing operation", async () => {
    const { engine, transport } = fixture();
    await engine.handle(request());
    expect((await engine.handle(request("send", {
      trade_url: "https://steamcommunity.com/tradeoffer/new/?partner=321",
    }))).error?.code).toBe("operation_conflict");
    expect((await engine.handle(request("status", { account_id: "wrong" }))).error?.code)
      .toBe("operation_conflict");
    expect(transport.sendCount).toBe(1);
  });
  it("failed confirmation remains unknown until an authoritative known-ID lookup", async () => {
    const { engine, transport } = fixture({ ...populated, send_status: "pending",
      confirm_error: true, reconcile_state: "sent" });
    await engine.handle(request());
    expect((await engine.handle(request("confirm"))).result).toMatchObject({
      state: "unknown_outcome", offer_id: "900000001" });
    await engine.handle(request("confirm"));
    expect(transport.confirmCount).toBe(1);
    expect((await engine.handle(request("reconcile", {}, { admission: undefined }))).result)
      .toMatchObject({ state: "sent" });
    expect(transport.sendCount).toBe(1);
  });
});
describe("inventory completeness and limits", () => {
  it("collects multiple pages/inventories, dedupes identities, drops partial inventory", async () => {
    const { engine, transport } = fixture({ pages: {
      "730/2": [{ assets: [asset("1"), asset("2")] }, { assets: [asset("2"), asset("3")] }],
      "440/2": [{ assets: [asset("9", 440)] }, { error: true }],
    }});
    const result = await engine.handle(request("inventory", { inventories: [
      { app_id: 730, context_id: "2" }, { app_id: 440, context_id: "2" },
      { app_id: 730, context_id: "2" },
    ]}));
    expect(result.result).toEqual({ assets: [asset("1"), asset("2"), asset("3")],
      failures: [{ app_id: 440, context_id: "2", code: "inventory_failed" }], simulation: true });
    expect(transport.sendCount).toBe(0);
  });
  it("allows legacy partial send but rejects it when caller requests all inventories", async () => {
    const scenario = { pages: { ...populated.pages, "440/2": [{ error: true }] } };
    for (const allow of [true, false]) {
      const { engine, transport } = fixture(scenario);
      const result = await engine.handle(request("send", { allow_partial: allow, inventories: [
        { app_id: 730, context_id: "2" }, { app_id: 440, context_id: "2" },
      ]}));
      expect(result.result).toMatchObject({ state: allow ? "sent" : "failed", code: "partial_inventory" });
      expect(transport.sendCount).toBe(allow ? 1 : 0);
    }
  });
  it("empty inventory cannot send an empty offer", async () => {
    const { engine, transport } = fixture({});
    expect((await engine.handle(request())).result).toMatchObject({ state: "failed", code: "no_items" });
    expect(transport.sendCount).toBe(0);
  });
  it("repeated pagination cursor is an explicit whole-inventory failure", async () => {
    const { engine } = fixture({ pages: { "730/2": [{ assets: [asset("1")], next: "0" }] } });
    expect((await engine.handle(request("inventory"))).result).toMatchObject({ assets: [],
      failures: [{ code: "pagination_loop" }] });
  });
});
describe("admission, deadlines, cancellation", () => {
  it("defaults closed and never opens transport without admission", async () => {
    const { engine, transport } = fixture();
    expect((await engine.handle(request("send", {}, { admission: undefined }))).error?.code)
      .toBe("admission_required");
    expect(transport.sendCount).toBe(0);
  });
  it("expired admission just before dispatch prevents sending", async () => {
    const { engine, transport } = fixture({ ...populated, delay_ms: 30 });
    const result = await engine.handle(request("send", {}, {
      admission: { expires_at_ms: Date.now() + 20 },
    }));
    expect(result.result).toMatchObject({ state: "not_sent", code: "admission_expired" });
    expect(transport.sendCount).toBe(0);
  });
  it("cancellation before send and deadline after dispatch have different states", async () => {
    const before = fixture({ ...populated, delay_ms: 100 });
    const req = request(); const running = before.engine.handle(req);
    await before.engine.handle(parseRequest({ version: 1, request_id: randomUUID(),
      method: "cancel", timeout_ms: 1000, params: { target_request_id: req.request_id } }));
    expect((await running).result).toMatchObject({ state: "not_sent", code: "cancelled" });
    expect(before.transport.sendCount).toBe(0);
    const after = fixture({ ...populated, send_delay_ms: 100 });
    const result = await after.engine.handle(request("send", {}, { timeout_ms: 15 }));
    expect(result.result).toMatchObject({ state: "unknown_outcome", code: "deadline_exceeded" });
    expect(after.transport.sendCount).toBe(1);
    expect((await after.engine.handle(request())).result).toMatchObject({ state: "unknown_outcome" });
    expect(after.transport.sendCount).toBe(1);
  });
  it("does not expose credential material in result or persisted public metadata", async () => {
    const { engine } = fixture({ ...populated, send_error: true });
    const result = await engine.handle(request());
    const text = JSON.stringify(result.result as TradeResult);
    for (const secret of Object.values(credentials)) expect(text).not.toContain(secret);
  });
});
