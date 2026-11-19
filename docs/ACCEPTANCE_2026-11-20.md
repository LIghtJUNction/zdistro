# zdistro native packaging acceptance — 2026-11-20

## Scope and method

The acceptance target is the package artifact, not installation, signing, registry publication, store approval, or runtime behavior on end-user devices. This report reviews the exact source snapshot, executes the repository's native smoke workflow, inspects the persisted job outcomes, and separates local Linux observations from hosted Windows, macOS, and Android validation.

The reviewed candidate is [`4d656faaa50e125b36060c3e08c2efbb31575c45`](https://github.com/LIghtJUNction/zdistro/commit/4d656faaa50e125b36060c3e08c2efbb31575c45). Its [manually dispatched workflow](https://github.com/LIghtJUNction/zdistro/actions/runs/44637462072) was created on 2026-11-19 at 23:24:59 UTC and completed at 23:25:42 UTC. All seven jobs concluded successfully.

| Hosted job | Observed result | What it checks |
| --- | --- | --- |
| `contracts (ubuntu-latest)` | Success | Python unit contracts |
| `contracts (windows-latest)` | Success | Python unit contracts, including subprocess-free planning |
| `contracts (macos-latest)` | Success | Python unit contracts |
| `linux-packages` | Success | Deb, Arch, ZIP, tar.gz, Cargo crate, wheel, sdist, npm, extension, XPI and Docker artifact paths |
| `windows-packages` | Success | MSI, MSIX, ZIP, tar.gz, extension and XPI artifacts |
| `macos-packages` | Success | PKG, DMG, ZIP, tar.gz, extension and XPI artifacts |
| `android-sdk-gate` | Success | APK and AAB sample packaging with the hosted SDK and bundletool |

These results and downloadable artifacts are recorded by the workflow itself. For the configured smoke samples, the test reruns receipt verification; it records format-level integrity or native inspection rather than claiming successful installation.

## Failures isolated and resolved

The earlier [failed run](https://github.com/LIghtJUNction/zdistro/actions/runs/44634799667) exposed three independent platform issues. Windows `plan` called `platform.system()`, whose Windows implementation used a subprocess despite the plan's no-command contract; [`66656d7`](https://github.com/LIghtJUNction/zdistro/commit/66656d74b1b5ad64ae560ca8944d4f8ccf880343) switched to `sys.platform`. Windows MakeAppx rejected the generated MSIX identity containing an underscore; [`1fd603d`](https://github.com/LIghtJUNction/zdistro/commit/1fd603d71ae8f2b54eec8eb2289bed1375cfbf95) and the subsequent integrated [`3faa64f`](https://github.com/LIghtJUNction/zdistro/commit/3faa64ff83ebb1830a982e87967552ca4eace0cf) retained a valid template identity. Ubuntu's Arch inspection attempted to initialize the system pacman database; [`f2bfbaa`](https://github.com/LIghtJUNction/zdistro/commit/f2bfbaa42fc8cbf8872dfc762854f1a7af1a105c) introduced a private per-build database.

The development host additionally reproduced a Debian packaging failure with a restrictive process umask: `DEBIAN/` was created with permission `0700` and `dpkg-deb` refused it. [`8f990a5`](https://github.com/LIghtJUNction/zdistro/commit/8f990a5642a56c3117d9f12b8d400c1250b0d3f9) explicitly normalizes control-directory and metadata permissions. On the next hosted [Linux failure](https://github.com/LIghtJUNction/zdistro/actions/runs/44636907033), Docker's default Buildx driver could not export a Docker archive; [`4d656fa`](https://github.com/LIghtJUNction/zdistro/commit/4d656faaa50e125b36060c3e08c2efbb31575c45) provisioned an isolated `docker-container` BuildKit driver for the acceptance job.

## Independent development-host checks

On the Linux development machine, `python -m unittest discover -s tests -q` passed 19 tests after the integrated validation work. Two independent native smoke invocations each passed four targets with persisted, reread receipts: `deb,arch,zip,tar-gz` and `npm,extension,xpi,apk`. The Deb and Arch checks invoked actual `dpkg-deb`, `pacman` and `bsdtar`; APK inspection invoked AAPT2. These eight local results are Linux evidence; other operating-system claims are grounded in the linked hosted jobs.

## Claim–evidence review

| Claim | Evidence | Status |
| --- | --- | --- |
| The exact candidate completed all seven configured hosted jobs | [Run 44637462072](https://github.com/LIghtJUNction/zdistro/actions/runs/44637462072) | Supported |
| Affected Windows, Arch, Debian, and Docker CI paths recovered | Failed-run logs, linked fix commits, and the green acceptance run | Supported for tested samples |
| Sample packages are installable, signed or accepted by app stores | No installation, signing or store-submission gate was executed | Needs further evidence |
| Every supported host/SDK configuration works | Hosted runner combinations and sample manifests cover a subset | Needs further evidence |

## Remaining gates

Package installation and removal tests, actual runtime smoke, signed Windows/macOS/Android distribution, store/registry publication, and non-runner SDK configurations remain separately verifiable work. The receipt matrix should continue to distinguish artifact integrity, native format checks, and host tool availability; a cross-compiled or inspected package must not be labeled as installed or released.

The countercheck for this report is whether each success can be traced to a run, source revision, named target and explicit verification scope. All acceptance claims above meet that test; broader distribution claims have been withheld.
