import { afterEach, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Journal } from "../src/journal.ts";
import { Engine } from "../src/engine.ts";
import { SimulationTransport } from "../src/simulation.ts";
import { parseRequest } from "../src/protocol.ts";
import type { Scenario } from "../src/simulation.ts";
import type { Asset } from "../src/types.ts";

const cleanups: (() => void)[] = [];
function fixture(scenario: Scenario = {}) {
  const dir = mkdtempSync(join(tmpdir(), "fsm-embedded-"));
  const journal = new Journal(join(dir,"worker.sqlite"),true);
  const transport = new SimulationTransport(scenario);
  const engine = new Engine(journal, () => transport);
  cleanups.push(() => { journal.close(); rmSync(dir,{ recursive:true,force:true }); });
  return { journal,transport,engine };
}
const credentials = { account_name:"synthetic",password:"synthetic-private",shared_secret:"synthetic-private" };
function request(method="activity",params={}, extra={}) {
  return parseRequest({ version:1,request_id:randomUUID(),method,timeout_ms:1000,
    admission:{ expires_at_ms:Date.now()+30_000 },params:{ account_id:"account-a",operation_id:"intent-a",
      credentials,app_ids:[730,440,730],duration_ms:60_000,machine_name:"Synthetic",...params },...extra });
}
afterEach(() => { for (const close of cleanups.splice(0)) close(); });
it("selected assets bypass inventory fetch, deduplicate, and carry comment only as send input", async () => {
  const {engine,transport}=fixture({ pages:{"730/2":[{error:true}]} });
  const calls:{assets:Asset[];comment:string|undefined}[]=[];
  const send=transport.send.bind(transport);
  transport.send=async (url,assets,signal,comment?:string) => { calls.push({assets,comment}); return send(url,assets,signal); };
  const item={app_id:730,context_id:"2",asset_id:"123",amount:1};
  const result=await engine.handle(request("send",{trade_url:"https://steamcommunity.com/tradeoffer/new/?partner=123",
    selected_assets:[item,item],comment:"Synthetic comment"}));
  expect(result.result).toMatchObject({state:"sent"});
  expect(calls).toEqual([{assets:[item],comment:"Synthetic comment"}]);
  expect(JSON.stringify(result)).not.toContain("Synthetic comment");
});
it("activity asks free app licences once, warms up, reports activity and clears it", async () => {
  const {engine,transport}=fixture({activity_delay_ms:0});
  expect((await engine.handle(request())).result).toMatchObject({state:"completed",granted_app_ids:[730,440]});
  expect(transport.played).toEqual([[730,440],[]]);
  expect((await engine.handle(request())).result).toMatchObject({state:"completed"});
  expect(transport.freeLicenseCount).toBe(1);
});
it("unknown free-licence response blocks another activity ID and is not treated as absence", async () => {
  const {engine,transport}=fixture({free_license_error:true});
  expect((await engine.handle(request())).result).toMatchObject({state:"unknown_outcome"});
  expect((await engine.handle(request("activity",{operation_id:"new-intent"}))).error?.code).toBe("unresolved_operation");
  expect((await engine.handle(request("activity_status",{}, {admission:undefined}))).result).toMatchObject({state:"unknown_outcome"});
  expect(transport.freeLicenseCount).toBe(1);
});
it("admission expiration during warmup prevents gamesPlayed and cleans up", async () => {
  const {engine,transport}=fixture({activity_delay_ms:200});
  expect((await engine.handle(request("activity",{}, {admission:{expires_at_ms:Date.now()+50}}))).result)
    .toMatchObject({state:"cancelled",code:"admission_expired"});
  expect(transport.played).toEqual([[]]);
});
it("renewal keeps admitted activity alive, cancellation clears a running presence", async () => {
  const {engine,transport}=fixture({activity_delay_ms:100});
  const initial=request("activity",{}, {admission:{expires_at_ms:Date.now()+80}});
  const running=engine.handle(initial);
  await new Promise(resolve=>setTimeout(resolve,15));
  expect((await engine.handle(parseRequest({version:1,request_id:randomUUID(),method:"renew",timeout_ms:1000,
    params:{target_request_id:initial.request_id},admission:{expires_at_ms:Date.now()+2000}}))).result).toEqual({renewed:true});
  for(let attempt=0;attempt<100 && transport.played.length===0;attempt++) await new Promise(resolve=>setTimeout(resolve,5));
  expect(transport.played[0]).toEqual([730,440]);
  await engine.handle(parseRequest({version:1,request_id:randomUUID(),method:"cancel",timeout_ms:1000,
    params:{target_request_id:initial.request_id}}));
  expect((await running).result).toMatchObject({state:"cancelled",code:"cancelled"});
  expect(transport.played.at(-1)).toEqual([]);
});
