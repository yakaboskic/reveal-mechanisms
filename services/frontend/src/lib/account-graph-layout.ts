import { hierarchy, pack } from "d3-hierarchy";
import type { AccountGraphNode } from "./account-graph";

function sizeVariation(id: string, spread: number): number {
  let hash = 2166136261;
  for (let index = 0; index < id.length; index++) hash = Math.imul(hash ^ id.charCodeAt(index), 16777619);
  return 1 + ((hash >>> 0) / 0xffffffff * 2 - 1) * spread;
}

export function layoutAccountHierarchy(root: AccountGraphNode) {
  const tree = hierarchy(root), scales = new Map<string, number>();
  // Seed visual variation by display occurrence, including its ancestors, so
  // claims sharing the same datasets do not all collapse to equal-sized circles.
  // D3 packs the resulting leaf radii and encloses them; never resize afterward.
  tree.eachBefore(node => {
    const inherited = node.parent ? scales.get(node.parent.data.id)! : 1;
    const spread = node.data.kind === "claim" ? 0.25 : node.data.kind === "dataset" ? 0.18 : 0.12;
    scales.set(node.data.id, node.parent ? inherited * sizeVariation(node.data.id, spread) : 1);
  });
  tree.sum(node => node.children.length ? 0 : Math.max(0.65, Math.min(1.45, scales.get(node.id)!)) ** 2)
    .sort((a, b) => (b.value || 0) - (a.value || 0) || a.data.id.localeCompare(b.data.id));
  return pack<AccountGraphNode>().size([1000, 1000])
    .padding(node => node.depth === 0 ? 20 : node.depth === 1 ? 50 : 18)(tree).descendants();
}
