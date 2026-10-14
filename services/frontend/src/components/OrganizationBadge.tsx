import { organizationInitials, organizationLogo, rorHref, type CreditedOrganization } from "@/lib/organizations";
import "./organization-badge.css";

const roleLabel = (role: string) => role.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());

/** An organization credited for a dataset: its logo (or initials), name and recorded roles, linked to its ROR record.
 * Compact badges show only the mark; the name stays available to assistive technology and as a tooltip. */
export function OrganizationBadge({ organization, roles = organization.roles, compact = false }: { organization: CreditedOrganization; roles?: string[]; compact?: boolean }) {
  const logo = organizationLogo(organization), name = organization.name || "Organization", href = rorHref(organization.ror);
  const credit = roles?.length ? roles.map(roleLabel).join(", ") : "";
  const tooltip = credit ? `${name} · ${credit}` : name;
  const content = <>
    {logo ? <img className={`organization-badge-logo${logo.wide ? " is-wide" : ""}`} src={logo.src} alt="" loading="lazy" decoding="async" />
      : <span className="organization-badge-initials" aria-hidden="true">{organizationInitials(name)}</span>}
    <span className={compact && logo ? "sr-only" : "organization-badge-name"}>{name}</span>
    {!compact && credit && <small className="organization-badge-roles">{credit}</small>}
    {compact && credit && <span className="sr-only">, {credit}</span>}
  </>;
  const className = `organization-badge${compact ? " is-compact" : ""}`;
  return href ? <a className={className} href={href} target="_blank" rel="noopener noreferrer" title={`${tooltip} (ROR record)`}>{content}</a>
    : <span className={className} title={tooltip}>{content}</span>;
}
