/** Logos and links for organizations credited in dataset lineage. Logo sources: public/org-logos/SOURCES.md. */
export type CreditedOrganization = { id: string; name?: string | null; ror?: string | null; roles?: string[] };
export type OrganizationLogo = { src: string; wide: boolean };

const logo = (file: string, wide = true): OrganizationLogo => ({ src: `/org-logos/${file}`, wide });
// ROR identifies an organization exactly; organizations without one match their exact recorded name.
const byRor: Record<string, OrganizationLogo> = {
  "ror:05a0ya142": logo("broad-institute.png"),
  "ror:02meqm098": logo("ncbi.svg", false),
  "ror:01kg8sb98": logo("indiana-university.svg"),
  "ror:04a9tmd77": logo("mount-sinai.png"),
};
const byName: Record<string, OrganizationLogo> = {
  "gtex consortium": logo("gtex.png"),
  "hubmap consortium": logo("hubmap.png", false),
  "ma'ayan laboratory, icahn school of medicine at mount sinai": logo("maayan-lab.jpg", false),
  "motrpac study group": logo("motrpac.png"),
};
const normalized = (name?: string | null) => (name || "").replace(/[‘’]/g, "'").replace(/\s+/g, " ").trim().toLowerCase();

export function organizationLogo(organization: CreditedOrganization): OrganizationLogo | null {
  return (organization.ror ? byRor[organization.ror] : undefined) || byName[normalized(organization.name)] || null;
}

/** Up to two initials from the significant words of a name, for organizations without a logo. */
export function organizationInitials(name?: string | null) {
  const words = (name || "").split(/[\s,]+/).filter(word => word && !/^(of|the|and|at|for|in)$/i.test(word));
  return words.slice(0, 2).map(word => [...word][0].toUpperCase()).join("") || "?";
}

/** The public ROR record of an organization, only for a well-formed ROR id. */
export const rorHref = (ror?: string | null) => ror && /^ror:0[a-z0-9]{8}$/.test(ror) ? `https://ror.org/${ror.slice(4)}` : null;
