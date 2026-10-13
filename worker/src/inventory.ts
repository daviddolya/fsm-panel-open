import { checkpoint, safeCode, WorkerError } from "./types.ts";
import type { Asset, InventoryResult, InventorySpec, Transport } from "./types.ts";

export async function collectInventory(transport: Transport, specs: InventorySpec[],
                                       signal: AbortSignal): Promise<InventoryResult> {
  const unique = [...new Map(specs.map(s => [`${s.app_id}/${s.context_id}`, s])).values()];
  let visitedAssets = 0;
  const results = await Promise.all(unique.map(async spec => {
    const items = new Map<string, Asset>();
    const cursors = new Set<string>();
    let cursor: string | undefined;
    try {
      for (let page = 0; ; page++) {
        checkpoint(signal);
        if (page >= 500) throw new WorkerError("inventory_limit");
        const response = await transport.inventory(spec, cursor, signal);
        checkpoint(signal);
        for (const item of response.assets) {
          if (++visitedAssets > 20_000) throw new WorkerError("inventory_limit");
          if (item.app_id !== spec.app_id || item.context_id !== spec.context_id ||
              !/^[1-9][0-9]{0,19}$/.test(item.asset_id) ||
              !Number.isSafeInteger(item.amount) || item.amount < 1) {
            throw new WorkerError("invalid_inventory");
          }
          const prior = items.get(item.asset_id);
          if (prior && prior.amount !== item.amount) throw new WorkerError("invalid_inventory");
          items.set(item.asset_id, {
            app_id: item.app_id, context_id: item.context_id,
            asset_id: item.asset_id, amount: item.amount,
          });
        }
        if (!response.next) break;
        if (cursors.has(response.next)) throw new WorkerError("pagination_loop");
        cursors.add(response.next);
        cursor = response.next;
      }
      return { assets: [...items.values()], failure: null };
    } catch (error) {
      checkpoint(signal);
      // An incomplete inventory contributes no assets, even if an earlier page worked.
      return { assets: [], failure: { ...spec, code: safeCode(error, "inventory_failed") } };
    }
  }));
  checkpoint(signal);
  return {
    assets: results.flatMap(r => r.assets),
    failures: results.flatMap(r => r.failure ? [r.failure] : []),
    simulation: transport.simulation,
  };
}
