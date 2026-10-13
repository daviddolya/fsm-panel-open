import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { Journal } from "./journal.ts";
import { Engine } from "./engine.ts";
import { parseRequest } from "./protocol.ts";
import { safeCode, VERSION, WorkerError } from "./types.ts";
import { SimulationTransport } from "./simulation.ts";
import type { Scenario } from "./simulation.ts";
import type { Response } from "./types.ts";

const diagnostic = (code: string) => process.stderr.write(JSON.stringify({ code }) + "\n");
// Upstream logging must never contaminate IPC or print SDK errors/cookies/passwords.
for (const key of ["log", "warn", "error", "info", "debug", "trace"] as const) {
  console[key] = () => { diagnostic("sdk_diagnostic_suppressed"); };
}
process.on("uncaughtException", () => { diagnostic("worker_fatal"); process.exit(2); });
process.on("unhandledRejection", () => { diagnostic("worker_fatal"); process.exit(2); });
process.stdout.on("error", () => process.exit(2));

async function main() {
  const { values } = parseArgs({ options: {
    state: { type: "string" }, mode: { type: "string", default: "simulation" },
    scenario: { type: "string" }, "allow-live-stand": { type: "boolean", default: false },
  }, strict: true, allowPositionals: false });
  if (!values.state || !["simulation", "live"].includes(values.mode!)) {
    throw new WorkerError("invalid_request");
  }
  if (values.mode === "live" && (!values["allow-live-stand"] || values.scenario)) {
    throw new WorkerError("live_stand_required");
  }
  const simulation = values.mode === "simulation";
  const scenario = values.scenario ? JSON.parse(readFileSync(values.scenario, "utf8")) as Scenario : {};
  const factory = simulation ? () => new SimulationTransport(scenario) :
    await import("./live.ts").then(({ LiveTransport }) => () => new LiveTransport());
  const journal = new Journal(values.state, simulation);
  const engine = new Engine(journal, factory);
  const pending = new Set<Promise<void>>();
  let writeChain = Promise.resolve();
  const write = (response: Response) => {
    writeChain = writeChain.then(() => new Promise<void>((resolve, reject) => {
      process.stdout.write(JSON.stringify(response) + "\n", error => error ? reject(error) : resolve());
    }));
    return writeChain;
  };
  const protocolError = (code: string) => write({ version: VERSION, request_id: null,
    ok: false, error: { code } });
  // Bound framing before parsing, including a peer that never writes a newline.
  let buffer = Buffer.alloc(0);
  const limit = 65_536;
  let closed = false;
  const shutdown = () => {
    if (closed) return;
    closed = true;
    engine.stop();
    void Promise.allSettled([...pending]).then(async () => {
      await writeChain.catch(() => {});
      journal.close();
      process.exit(0);
    });
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  process.stdin.on("end", shutdown);
  process.stdin.on("error", shutdown);
  process.stdin.on("data", (chunk: Buffer) => {
    if (closed) return;
    buffer = Buffer.concat([buffer, chunk]);
    while (true) {
      const newline = buffer.indexOf(10);
      if (newline < 0) break;
      const frame = buffer.subarray(0, newline);
      buffer = buffer.subarray(newline + 1);
      if (frame.length > limit) { void protocolError("frame_too_large"); shutdown(); return; }
      if (pending.size >= 64) { void protocolError("worker_busy"); shutdown(); return; }
      let request;
      try {
        let input;
        try { input = JSON.parse(frame.toString("utf8")); }
        catch { throw new WorkerError("invalid_json"); }
        request = parseRequest(input);
      } catch (error) { void protocolError(safeCode(error)); continue; }
      const task = engine.handle(request).then(write);
      pending.add(task);
      void task.finally(() => pending.delete(task));
    }
    if (buffer.length > limit) { void protocolError("frame_too_large"); shutdown(); }
  });
}
main().catch(error => { diagnostic(safeCode(error)); process.exit(2); });
