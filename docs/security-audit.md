# Dependency advisory review

Reviewed **2026-10-22** against the locked core and optional GUI dependencies
using uv 0.13.1 and OSV; development/Graphify groups and legacy DP5 are excluded.
Review again by **2026-11-22**, before release, or when dependencies or their use change;
known advisory matches remain, and reachability assessments are not exploit tests.

```sh
uv audit --frozen --no-default-groups --output-format json --no-python-downloads
```

## Remediated HTTP dependencies

Floors remain `certifi>=2026.10.19` and `urllib3>=2.8.0,<3`, including
unlocked installs. Other compatibility constraints remain unchanged.

Name resolution now uses only local OPSIN, with no HTTP endpoint or network fallback.
The original audit counts below are retained; its resolver-specific reachability
assessments have been updated for this removal without rerunning the dependency audit.

| Advisory group | Application path and disposition |
| --- | --- |
| [certifi GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) | The OPSIN HTTPS path has been removed; name resolution no longer reaches this dependency. The 2026.10.19 fix removes the compromised trust chain while retaining unaffected emSign roots. |
| [urllib3 GHSA-gh4c-6fx4-qh6g](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g), [GHSA-vxq7-64xx-v4gw](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw) | The Requests response path has been removed from name resolution. The 2.8.0 fix covers Deflate-loop and unbounded chunk-size-line fixes. |
| urllib3 GHSA-qccp-gfcp-xxvc, GHSA-mf9v-mfxr-j63j, GHSA-8988-9cw3-xx77 | Low-level redirect, decompression and HTTPS-proxy matches also removed by the same 2.8.0 update. No separate application exploit was reproduced. |

The reviewed audit contained **40 packages, 85 raw advisory records and 62 groups**
after alias grouping, with no certifi/urllib3 matches.

## Remaining dispositions

All rows below are deferred as of the review date above, with the same review
deadline. A changed usage invalidates its reachability assessment.

| Locked package; advisory groups | Reachability reason and limits |
| --- | --- |
| idna 3.11; [GHSA-65pc-fj4g-8rjx](https://github.com/advisories/GHSA-65pc-fj4g-8rjx) (1 group, 2 records) | **Deferred; no resolver HTTP path remains.** Name resolution uses local OPSIN only, so it does not process a URL or host through idna. This removal does not remediate idna or establish safety for other dependency call paths. No exploit was reproduced. Reassess any new network client or host before release. Fix is idna 3.15. |
| lxml 6.0.2; [GHSA-vfmq-68hx-4jfw](https://github.com/advisories/GHSA-vfmq-68hx-4jfw) (1 group, 2 records) | python-docx 1.2.0 explicitly uses `etree.XMLParser(resolve_entities=False)`. Project source does not call the affected default `iterparse`/`ETCompatXMLParser` APIs. Reassess if parser selection/options change. Fix is lxml 6.1.0. |
| Pillow 12.1.0; 58 groups listed below (77 records) | No project path calls Pillow's affected image/font decoders, transforms, encoders or viewers. `reporting.generate_molecule_image` uses RDKit `MolDraw2DCairo` and returns PNG bytes; document/report processing uses PyMuPDF and python-docx. Importing RDKit drawing helpers can load Pillow, but does not establish an untrusted decoding path. Reassess any new Pillow API use or upstream adapter change. Current complete fix set requires Pillow 13.0.0. |
| requests 2.32.5; [GHSA-gc5v-m9x4-r6x2](https://github.com/advisories/GHSA-gc5v-m9x4-r6x2) (1 group, 2 records) | No project call to `extract_zipped_paths` remains, and name resolution no longer imports Requests. Embedded or zip-based installs and custom CA-bundle configuration need reassessment. Fix is Requests 2.33.0. |
| setuptools 80.10.2; [GHSA-h35f-9h28-mq5c](https://github.com/advisories/GHSA-h35f-9h28-mq5c) (1 group, 2 records) | Preserve `<81` for pyopsin's `pkg_resources` dependency. Build helpers stage Git-tracked inputs only and reject symlinks; this repository has no `MANIFEST.in` exclusion policy susceptible to the NFC/NFD collision. Plain/direct upstream builds need their own input review. Reassess build-policy changes and upgrade when pyopsin compatibility allows; advisory fix is setuptools 83.0.0. |

The following Pillow advisory IDs all share the Pillow disposition above. OSV
aliases (CVE/PYSEC/BIT) are grouped with these IDs, not counted as separate defects:

```text
GHSA-22hg-r758-rrw9 GHSA-24rc-8hj2-v98c GHSA-254f-3936-288h GHSA-27h8-gf7j-v5hc
GHSA-332p-7hrc-92wf GHSA-3j72-p6qg-6xp7 GHSA-4558-3h8r-v8xp GHSA-45hq-cxwh-f6vc
GHSA-462x-pgg6-55fp GHSA-4rm8-3hmm-39fg GHSA-4rr5-89j8-mwwm GHSA-4x4j-2g7c-83w6
GHSA-58r4-6rv3-gfh8 GHSA-5x94-69rx-g8h2 GHSA-5xm2-6676-qcp9 GHSA-5xmw-vc9v-4wf2
GHSA-62p4-gmf7-7g93 GHSA-65qm-jvmh-6pqg GHSA-6r8x-57c9-28j4 GHSA-74r2-7ff9-mjj2
GHSA-78g4-xqwv-q3g8 GHSA-7mwm-ghpf-9vrm GHSA-8g5g-8vmr-9782 GHSA-8v84-f9pq-wr9x
GHSA-9hw9-ch79-4vh6 GHSA-9v9v-9qvf-h2hj GHSA-c6hw-5grf-ff96 GHSA-cfh3-3jmp-rvhc
GHSA-cp9v-662q-vvx9 GHSA-f2g6-542v-9j58 GHSA-fc9g-5qg7-mf45 GHSA-fj7v-r99m-22gq
GHSA-g4qv-2jxx-769h GHSA-g7cr-3p24-7wf7 GHSA-g842-9rc4-69pw GHSA-gg2r-wvfm-q9rx
GHSA-j4m9-6w36-pw47 GHSA-jg86-v3jv-hrpm GHSA-jjj6-mw9f-p565 GHSA-mrfp-p49p-3hf2
GHSA-p4wg-p73j-c528 GHSA-p8hx-v2p6-rp84 GHSA-pchr-8j38-3x4p GHSA-pg7v-jwj7-p798
GHSA-phj9-mv4w-65pm GHSA-pwv6-vv43-88gr GHSA-q45j-58j2-45rg GHSA-r73j-pqj5-w3x7
GHSA-rfgq-q3w2-pmv2 GHSA-v9pv-5gc8-w2m6 GHSA-vjc4-5qp5-m44j GHSA-vx6w-vv9f-6gvg
GHSA-w889-685w-c8fh GHSA-whj4-6x5x-4v2j GHSA-wjx4-4jcj-g98j GHSA-xgh8-jp6q-p5x8
GHSA-xj96-63gp-2gmr GHSA-xvh5-g6gv-559v
```
