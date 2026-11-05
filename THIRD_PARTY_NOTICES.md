# Third-party notices

The root MIT license applies to the extracted runner and new reporting/package code. It does not replace original project licenses in task snapshots, patches, cloned repositories, test data, dependencies or container images.

| Task source | Count | License | License text |
|---|---:|---|---|
| Django | 6 | BSD-3-Clause | `licenses/django.txt` |
| Docusaurus | 3 | MIT | `licenses/docusaurus.txt` |
| Vue | 3 | MIT | `licenses/vue.txt` |
| fmt | 1 | MIT with optional compiled-object exception | `licenses/fmt.txt` |
| nlohmann/json | 1 | MIT | `licenses/json.txt` |
| Tokio | 1 | MIT | `licenses/tokio.txt` |
| SWE-bench | Dataset/harness provenance | MIT, as declared by the project | `licenses/swe-bench.txt` |

Full upstream license texts were retrieved at the frozen task base revisions (SWE-bench at the separately recorded notice-source revision). `licenses/sources.json` records exact URLs and SHA-256 values. Identical copies are included inside the Python distribution at `coding_benchmark/assets/licenses/`.

Dataset source: [SWE-bench Lite](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) and [SWE-bench Multilingual](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual/tree/846e647b9f33c0b51b739d005d13d85493c9af09). Multilingual's dataset card declares MIT. SWE-bench's project license does not supersede original repository licenses on embedded patches/test code.

Please cite the SWE-bench and SWE-bench Multilingual authors when publishing research using these cases. See the upstream [citation instructions](https://github.com/SWE-bench/SWE-bench#citation--license).

Pi 1.4.0 (`@earendil-works/pi-coding-agent`, MIT) is installed separately by the setup command using the included npm lock; its source and native binaries are not bundled in the Python wheel. Its [upstream license](https://github.com/earendil-works/pi/blob/main/LICENSE) and transitive dependency licenses still apply. Docker, Python, Node.js, upstream cloned repositories and image contents remain external prerequisites with their own licenses. This is a task-source attribution inventory, not a transitive software bill of materials or a new license grant for issue discussion text.
