import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { organizationInitials, organizationLogo, rorHref } from "../src/lib/organizations";

test("credited organizations resolve their bundled logo by ROR id, else by exact recorded name", () => {
  assert.equal(organizationLogo({ id: "dapper:Organization.x", name: "Anything", ror: "ror:05a0ya142" })?.src, "/org-logos/broad-institute.png");
  assert.equal(organizationLogo({ id: "dapper:Organization.y", name: "Ma’ayan Laboratory, Icahn School of Medicine at Mount Sinai" })?.src, "/org-logos/maayan-lab.jpg");
  assert.equal(organizationLogo({ id: "dapper:Organization.z", name: "GTEx  Consortium" })?.src, "/org-logos/gtex.png");
  assert.equal(organizationLogo({ id: "dapper:Organization.q", name: "Unknown Data Center", ror: "ror:000000000" }), null);
  for (const [ror, name] of [["ror:05a0ya142", ""], ["ror:02meqm098", ""], ["ror:01kg8sb98", ""], ["ror:04a9tmd77", ""], ["", "HuBMAP Consortium"], ["", "MoTrPAC Study Group"], ["", "GTEx Consortium"], ["", "Ma'ayan Laboratory, Icahn School of Medicine at Mount Sinai"]]) {
    const logo = organizationLogo({ id: "x", name, ror: ror || null });
    assert.ok(logo && existsSync(new URL(`../public${logo.src}`, import.meta.url)), `${ror || name} has a bundled logo`);
  }
});

test("organizations without a logo show initials, and only well-formed ROR ids link out", () => {
  assert.equal(organizationInitials("National Center for Biotechnology Information"), "NC");
  assert.equal(organizationInitials("Institute of the Data"), "ID");
  assert.equal(organizationInitials(""), "?");
  assert.equal(rorHref("ror:02meqm098"), "https://ror.org/02meqm098");
  assert.equal(rorHref("ror:javascript:alert(1)"), null);
  assert.equal(rorHref(null), null);
});
