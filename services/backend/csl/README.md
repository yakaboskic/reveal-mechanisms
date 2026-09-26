# Pinned citation runtime

The backend runs **citeproc-js 2.4.63** in **QuickJS 1.19.4**, with the official APA and Modern Language Association styles and en-US locale. `manifest.json` records exact upstream commit URLs and SHA-256 checksums, checked before rendering. The processor archive was checked against its npm release digest. No network access or mutable style fetch occurs during rendering.

Processor: [Juris-M/citeproc-js](https://github.com/Juris-M/citeproc-js), authored by Frank Bennett. Its license notice is retained in `CITEPROC-LICENSE` and the source header; complete alternative terms are in `LICENSE-CPAL-1.0.txt` and `LICENSE-AGPL-3.0.txt`. CSL styles and locales retain their upstream license notices in their XML. Upstream repositories are [CSL styles](https://github.com/citation-style-language/styles) and [CSL locales](https://github.com/citation-style-language/locales).

Each rendering processes the entire saved Paragraph citation set, including disambiguation updates to earlier citations. Registry keys include exact metadata revisions. The wire manifest identifies processor, style/locale hashes, citation profile and metadata checksums. Numbered REVEAL export references remain a separate rendering from APA/MLA.
