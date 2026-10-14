# Organization logos

These marks identify the organizations credited for a dataset in its traced reference lineage, on the CFDE datasets in a scientific account graph's Source column. The same files are bundled with the REVEAL frontend (`services/frontend/public/org-logos`). They do not imply endorsement of REVEAL. Each file was downloaded unchanged on 2026-10-12. Each logo belongs to its organization.

| File | Organization | Source |
|---|---|---|
| `broad-institute.png` | Broad Institute (ROR `05a0ya142`) | The site logo of [broadinstitute.org](https://www.broadinstitute.org/): <https://www.broadinstitute.org/themes/custom/broad/logo.png> |
| `gtex.png` | GTEx Consortium | The GTEx logo in the header of the [GTEx Portal](https://gtexportal.org/home/): <https://gtexportal.org/img/gtex-logo.372cd1b4.png> |
| `ncbi.svg` | National Center for Biotechnology Information (ROR `02meqm098`) | [File:US-NLM-NCBI-Logo.svg](https://commons.wikimedia.org/wiki/File:US-NLM-NCBI-Logo.svg) on Wikimedia Commons, the logo Wikidata records for NCBI (Q82494). Public domain. |
| `hubmap.png` | HuBMAP Consortium | The site icon of [hubmapconsortium.org](https://hubmapconsortium.org/): <https://hubmapconsortium.org/wp-content/uploads/2019/01/android-icon-192x192-1.png>. HuBMAP's header wordmark is white only, so it would not show on REVEAL's light surfaces. |
| `indiana-university.svg` | Indiana University (ROR `01kg8sb98`) | [File:Indiana University logotype.svg](https://commons.wikimedia.org/wiki/File:Indiana_University_logotype.svg) on Wikimedia Commons, the logo Wikidata records for Indiana University (Q6608367). Public domain. |
| `mount-sinai.png` | Icahn School of Medicine at Mount Sinai (ROR `04a9tmd77`) | The site logo of [icahn.mssm.edu](https://icahn.mssm.edu/): <https://icahn.mssm.edu/sites/ismms/assets/images/MountSinai_IcahnSchool_Logo-horizontal.png> |
| `maayan-lab.jpg` | Ma'ayan Laboratory, Icahn School of Medicine at Mount Sinai | The avatar of the lab's GitHub organization [MaayanLab](https://github.com/MaayanLab): <https://github.com/MaayanLab.png?size=200>. The lab's website shows only the Mount Sinai logo. |
| `motrpac.png` | MoTrPAC Study Group | The site logo of [motrpac.org](https://www.motrpac.org/): <https://www.motrpac.org/assets/img/MoTrPAC_horizontal.png> |

`src/lib/organizations.ts` maps organizations to these files: by ROR id when the lineage records one, otherwise by the exact recorded name. An organization without a file shows its initials.
