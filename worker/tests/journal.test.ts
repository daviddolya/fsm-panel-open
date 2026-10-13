import { afterEach, expect, it } from "vitest";
import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Journal } from "../src/journal.ts";
import { parseRequest } from "../src/protocol.ts";

const directories: string[] = [];
const journals: Journal[] = [];
const request = () => parseRequest({ version: 1, request_id: "request-a", method: "send",
  timeout_ms: 1000, params: { account_id: "account-a", operation_id: "operation-a",
    credentials: { account_name: "synthetic", password: "synthetic", shared_secret: "synthetic" },
    trade_url: "https://steamcommunity.com/tradeoffer/new/?partner=123" } });
function path() {
  const directory = mkdtempSync(join(tmpdir(), "fsm-journal-")); directories.push(directory);
  return join(directory, "worker.sqlite");
}
afterEach(() => {
  for (const journal of journals.splice(0)) { try { journal.close(); } catch {} }
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
});
it("a concurrent owner cannot reset a preparing operation", () => {
  const file = path(); const first = new Journal(file, true); journals.push(first);
  first.reserve(request());
  expect(() => new Journal(file, true)).toThrow("worker_busy");
  expect(first.get("operation-a", "account-a").phase).toBe("preparing");
  expect(() => first.reserve({ ...request(), params: { ...request().params, operation_id: "new-id" } }))
    .toThrow("unresolved_operation");
  first.close();
  const reopened = new Journal(file, true); journals.push(reopened);
  expect(reopened.get("operation-a", "account-a").phase).toBe("idle");
});
it("rejects foreign or incomplete schemas and a missing primary key", () => {
  for (const sql of ["CREATE TABLE accounts(id TEXT)",
    "CREATE TABLE operations(operation_id TEXT); PRAGMA user_version=1;",
    `CREATE TABLE operations(operation_id TEXT, account_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL, partner_id TEXT NOT NULL,state TEXT NOT NULL,phase TEXT NOT NULL,
      offer_id TEXT,code TEXT,updated_at_ms INTEGER NOT NULL,simulation INTEGER NOT NULL);
      PRAGMA user_version=1;`]) {
    const file = path(); const db = new DatabaseSync(file); db.exec(sql); db.close();
    expect(() => new Journal(file, true)).toThrow("journal_error");
  }
});
it("rejects a same-named but weakened unfinished-operation index", () => {
  const file = path(); const first = new Journal(file, true); first.close();
  const db = new DatabaseSync(file);
  db.exec("DROP INDEX one_unresolved_offer_per_account; CREATE INDEX one_unresolved_offer_per_account ON operations(account_id)");
  db.close();
  expect(() => new Journal(file, true)).toThrow("journal_error");
});
it("a populated simulation journal cannot be opened for live work", () => {
  const file = path(); const first = new Journal(file, true); first.reserve(request()); first.close();
  expect(() => new Journal(file, false)).toThrow("journal_error");
});
