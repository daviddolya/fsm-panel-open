import { DatabaseSync } from "node:sqlite";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { safeCode, WorkerError } from "./types.ts";
import type { Request, TradeResult, TradeState } from "./types.ts";

type Row = TradeResult & { fingerprint: string; partner_id: string; phase: string };
export class Journal {
  private db: DatabaseSync;
  private owner = randomUUID();
  readonly simulation: boolean;
  constructor(path: string, simulation: boolean) {
    this.simulation = simulation;
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    try {
    this.db.exec("PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;");
    const version = this.db.prepare("PRAGMA user_version").get()?.user_version;
    if (version !== 0 && version !== 1) throw new WorkerError("journal_error");
    const objects = this.db.prepare(`SELECT type,name FROM sqlite_master
      WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')`).all();
    if ((version === 0 && objects.length) || objects.some(row => row.type !== "table" ||
      !["operations", "worker_owner"].includes(String(row.name)))) throw new WorkerError("journal_error");
    if (version === 1 && !objects.some(row => row.name === "operations")) throw new WorkerError("journal_error");
    this.db.exec(`CREATE TABLE IF NOT EXISTS operations (
      operation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
      partner_id TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL,
      offer_id TEXT, code TEXT, updated_at_ms INTEGER NOT NULL, simulation INTEGER NOT NULL
    ); PRAGMA user_version=1;`);
    if (this.db.prepare("PRAGMA quick_check").get()?.quick_check !== "ok") {
      throw new WorkerError("journal_error");
    }
    const expected = [
      ["operation_id","TEXT",0,1], ["account_id","TEXT",1,0], ["fingerprint","TEXT",1,0],
      ["partner_id","TEXT",1,0], ["state","TEXT",1,0], ["phase","TEXT",1,0],
      ["offer_id","TEXT",0,0], ["code","TEXT",0,0], ["updated_at_ms","INTEGER",1,0],
      ["simulation","INTEGER",1,0],
    ];
    const columns = this.db.prepare("PRAGMA table_info(operations)").all()
      .map(c => [c.name,c.type,c.notnull,c.pk]);
    if (JSON.stringify(columns) !== JSON.stringify(expected) ||
        !this.db.prepare("PRAGMA index_list(operations)").all().some(i => i.origin === "pk" && i.unique === 1) ||
        this.db.prepare("PRAGMA foreign_key_list(operations)").all().length) {
      throw new WorkerError("journal_error");
    }
    for (const row of this.db.prepare("SELECT * FROM operations").iterate()) {
      if (typeof row.operation_id !== "string" || !/^[a-zA-Z0-9_.:-]{1,128}$/.test(row.operation_id) ||
          typeof row.account_id !== "string" || !/^[a-zA-Z0-9_.:-]{1,128}$/.test(row.account_id) ||
          !/^[a-f0-9]{64}$/.test(String(row.fingerprint)) ||
          !/^[1-9][0-9]{0,19}$/.test(String(row.partner_id)) ||
          !["not_sent","created_pending_confirmation","confirmed","sent","failed","unknown_outcome"].includes(String(row.state)) ||
          !["idle","preparing","sending","confirming"].includes(String(row.phase)) ||
          (row.code !== null && safeCode(new WorkerError(String(row.code))) !== row.code) ||
          (row.offer_id !== null && !/^[1-9][0-9]{0,19}$/.test(String(row.offer_id))) ||
          !Number.isSafeInteger(row.updated_at_ms) || Number(row.updated_at_ms) < 0 ||
          row.simulation !== Number(simulation)) throw new WorkerError("journal_error");
    }
    const index = this.db.prepare(`SELECT sql FROM sqlite_master
      WHERE name='one_unresolved_offer_per_account'`).get();
    if (index && String(index.sql).replace(/\s+/g, "").replace(/IFNOTEXISTS/gi, "") !==
      "CREATEUNIQUEINDEXone_unresolved_offer_per_accountONoperations(account_id,simulation)WHEREstateIN('unknown_outcome','created_pending_confirmation')ORphase!='idle'") {
      throw new WorkerError("journal_error");
    }
    this.db.exec(`CREATE TABLE IF NOT EXISTS worker_owner (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner_pid INTEGER NOT NULL,
      nonce TEXT NOT NULL
    );`);
    const ownerColumns = this.db.prepare("PRAGMA table_info(worker_owner)").all()
      .map(c => [c.name,c.type,c.notnull,c.pk]);
    if (JSON.stringify(ownerColumns) !== JSON.stringify([
      ["singleton","INTEGER",0,1], ["owner_pid","INTEGER",1,0], ["nonce","TEXT",1,0],
    ])) throw new WorkerError("journal_error");
    this.db.exec("BEGIN IMMEDIATE");
    const owners = this.db.prepare("SELECT * FROM worker_owner").all();
    if (owners.length > 1 || (owners[0] && (owners[0].singleton !== 1 ||
        !Number.isSafeInteger(owners[0].owner_pid) || Number(owners[0].owner_pid) <= 0))) {
      throw new WorkerError("journal_error");
    }
    if (owners[0]) {
      let alive = true;
      try { process.kill(Number(owners[0].owner_pid), 0); }
      catch (error) { alive = (error as NodeJS.ErrnoException).code !== "ESRCH"; }
      if (alive) throw new WorkerError("worker_busy");
    }
    this.db.prepare("DELETE FROM worker_owner").run();
    this.db.prepare("INSERT INTO worker_owner VALUES(1,?,?)").run(process.pid, this.owner);
    // Preparing has not crossed the effect boundary. Sending/confirming is already
    // UNKNOWN, committed before dispatch; no startup path ever dispatches an offer.
    this.db.prepare(`UPDATE operations SET phase='idle', code='recovered_after_restart',
      updated_at_ms=? WHERE phase != 'idle'`).run(Date.now());
    this.db.exec(`CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_offer_per_account
      ON operations(account_id, simulation)
      WHERE state IN ('unknown_outcome','created_pending_confirmation') OR phase != 'idle';`);
    this.db.exec("COMMIT");
    } catch (error) {
      try { this.db.exec("ROLLBACK"); } catch { /* No transaction opened. */ }
      this.db.close();
      throw error instanceof WorkerError ? error : new WorkerError("journal_error");
    }
  }
  get(id: string, accountId: string): Row {
    const row = this.db.prepare("SELECT * FROM operations WHERE operation_id=?").get(id);
    if (!row) throw new WorkerError("operation_not_found");
    if (row.account_id !== accountId || Boolean(row.simulation) !== this.simulation) {
      throw new WorkerError("operation_conflict");
    }
    return { ...row, simulation: Boolean(row.simulation) } as Row;
  }
  reserve(request: Request): { row: Row; created: boolean } {
    const p = request.params;
    const inventories = [...new Map(p.inventories!.map(s =>
      [`${s.app_id}/${s.context_id}`, s])).values()].sort((a, b) =>
      `${a.app_id}/${a.context_id}`.localeCompare(`${b.app_id}/${b.context_id}`));
    const fingerprint = createHash("sha256").update(JSON.stringify([
      p.account_id, p.credentials!.account_name.toLowerCase(), p.trade_url,
      inventories, p.allow_partial ?? true, this.simulation,
    ])).digest("hex");
    const result = this.db.prepare(`INSERT OR IGNORE INTO operations VALUES
      (?,?,?,?, 'not_sent','preparing',NULL,NULL,?,?)`).run(
      p.operation_id!, p.account_id!, fingerprint,
      new URL(p.trade_url!).searchParams.get("partner")!, Date.now(), Number(this.simulation),
    );
    let row: Row;
    try { row = this.get(p.operation_id!, p.account_id!); }
    catch (error) {
      if (error instanceof WorkerError && error.code === "operation_not_found") {
        throw new WorkerError("unresolved_operation");
      }
      throw error;
    }
    if (row.fingerprint !== fingerprint) throw new WorkerError("operation_conflict");
    return { row, created: result.changes === 1 };
  }
  update(id: string, accountId: string, state: TradeState, code: string | null = null,
         offerId?: string, phase = "idle"): TradeResult {
    this.get(id, accountId);
    this.db.prepare(`UPDATE operations SET state=?, phase=?, code=?, updated_at_ms=?,
      offer_id=COALESCE(?, offer_id) WHERE operation_id=?`).run(
      state, phase, code, Date.now(), offerId ?? null, id);
    return this.public(this.get(id, accountId));
  }
  claim(id: string, accountId: string, expected: TradeState, phase: string): void {
    this.get(id, accountId);
    const result = this.db.prepare(`UPDATE operations SET state='unknown_outcome',
      phase=?, code=NULL, updated_at_ms=? WHERE operation_id=? AND state=?`).run(
      phase, Date.now(), id, expected);
    if (result.changes !== 1) throw new WorkerError("operation_conflict");
  }
  public(row: TradeResult): TradeResult {
    const { operation_id, account_id, state, offer_id, code, updated_at_ms, simulation } = row;
    return { operation_id, account_id, state, offer_id, code, updated_at_ms, simulation };
  }
  close(): void {
    this.db.prepare("DELETE FROM worker_owner WHERE nonce=?").run(this.owner);
    this.db.close();
  }
}
