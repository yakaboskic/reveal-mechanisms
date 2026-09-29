import type { Schema } from "@/lib/client";
import { mechanismName, mechanismTrait } from "@/lib/mechanism-display";
import "./mechanism-label.css";

export function MechanismLabel({ factor, sourceId }: { factor?: Schema<"EagglFactor">; sourceId?: string }) {
  const trait = mechanismTrait(factor, sourceId);
  return <span className="mechanism-label"><span className="mechanism-name">{mechanismName(factor)}</span>{trait && <span className="mechanism-trait">{trait}</span>}</span>;
}
