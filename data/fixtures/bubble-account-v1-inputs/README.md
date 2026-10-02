# Pinned DisMech input for the canonical bubble fixture

`Coronary_Artery_Disease.yaml` is the exact source file retained by the local DisMech import on October 1, 2026. Its SHA-256 is `00f2a71dca11fd7d4a8c835ea9e978037dcbef88d3fb050c5e74192db2bd7338`, matching the source revision returned by the local knowledge-gap API for `dismech:disorders/Coronary_Artery_Disease#discussion:cad_pgsxc_reverse_causation`.

The file was captured from `.deployment-assets/dismech/kb/disorders/Coronary_Artery_Disease.yaml`. The selected `/discussions/0` object is exactly equal to the retained discussion in `design/data/cad-account/sources/dismech-gap.source.json`, and the resulting canonical KnowledgeGap payload is unchanged. The API contract example has an older source revision and is not used as the revision authority.

`scripts/build_bubble_account.py` verifies this checksum and discussion equality offline, copies these bytes into the complete fixture, and includes them as a File in the source-gap Dataset. It never rebinds a fixture to a live source by matching text. The seed separately requires the target catalog's exact source ID, file revision, and canonical gap payload to match.
