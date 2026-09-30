import type { Schema } from "@/lib/client";
import { mechanismName, mechanismTrait } from "@/lib/mechanism-display";
import type { OutdatedAnchor } from "@/lib/reference";
import "./mechanism-label.css";

/** An outdated anchor renders from its archived stamp or frozen factor, never from the live catalog. */
export function MechanismLabel({ factor, sourceId, outdated }: { factor?: Schema<"EagglFactor">; sourceId?: string; outdated?: OutdatedAnchor }) {
  const trait = outdated ? outdated.trait : mechanismTrait(factor, sourceId);
  return <span className="mechanism-label"><span className="mechanism-name">{outdated ? outdated.name : mechanismName(factor)}</span>{(trait || outdated) && <span className="mechanism-trait">{trait}{outdated && <span className="mechanism-outdated">{trait ? " · " : ""}Outdated reference</span>}</span>}</span>;
}
