# Format backends and verification

zdistro delegates package construction to the ecosystem's own tools. A format
backend has four responsibilities: declare prerequisites, describe a plan,
produce an artifact, and verify that artifact. Language compilation happens
before file staging; cross compilation remains a language-toolchain setting.

## Built-in formats

| Target | Builder / required tools | Verification performed |
|---|---|---|
| `deb` | `dpkg-deb` | Package/version identity and payload listing |
| `arch` | `makepkg`, `fakeroot`, `pacman`, `bsdtar` | Native package inspection and `.PKGINFO`, `.BUILDINFO`, `.MTREE` |
| `zip`, `tar-gz` | Python standard archive libraries | Member boundaries, readable archive; ZIP CRCs |
| `crate` | `cargo package` | Cargo's default unpack/build verification, normalized manifest identity |
| `wheel` | `python -m build --wheel` | Core metadata and every wheel RECORD digest/size |
| `sdist` | `python -m build --sdist` | Source distribution archive and PKG-INFO identity |
| `npm` | `npm pack --ignore-scripts` | Tar structure and package.json identity |
| `extension`, `xpi` | ZIP library | Root manifest, required fields, local scripts/pages/icons, ZIP CRCs |
| `pkg` | macOS `pkgbuild`, `pkgutil` | Expand native metadata and payload without installing |
| `dmg` | macOS `hdiutil` | Image integrity and format inspection without mounting |
| `msi` | Windows WiX 4+ CLI | Native MSI database decompilation |
| `msix` | Windows SDK MakeAppx | ZIP/block-map metadata and native unpack |
| `apk` | Android AAPT2 + platform `android.jar`, or existing Gradle project | Binary manifest/resources parsed by AAPT2 |
| `aab` | Existing Gradle Android project, Java and bundletool | Bundle module validation by bundletool |
| `docker` | Docker Buildx or Podman | Docker/OCI archive references and OCI blob digests/sizes |

These are artifact checks. Installation/upgrade behavior, application behavior,
code signing, notarization and store review require their own workflows and are
not marked complete by an artifact receipt.

## Linux packages

Debian and Ubuntu packages are created with `dpkg-deb --root-owner-group --build`.
zdistro generates `DEBIAN/control` from the package metadata and records runtime
dependencies from `targets.deb.depends`. This follows the native tool's binary
package directory contract. See [dpkg-deb](https://manpages.debian.org/testing/dpkg/dpkg-deb.1.en.html).

Arch packages use a generated PKGBUILD whose package function copies the staged
files. `makepkg` generates the ALPM metadata and compressed archive. The backend
uses `--nodeps` because language builds and their tool checks have already been
performed before staging; declared runtime dependencies remain in the package.
No dependencies are installed into the host. `makepkg` requires an unprivileged
user. The output uses `.pkg.tar.gz`, a native makepkg-supported compression
choice. See [makepkg](https://man.archlinux.org/man/makepkg.8.en),
[ALPM package contents](https://man.archlinux.org/man/alpm-package.7.en) and
[PKGINFO](https://man.archlinux.org/man/PKGINFO.5.en).

`architecture = "all"` (Debian) and `architecture = "any"` (Arch) are suitable for
architecture-independent payloads. Select the actual architecture for compiled
binaries. Package metadata does not cross-compile the executable.

## Python, Rust and JavaScript

Python artifacts use the normal PEP 517 frontend and the project's declared
build backend. Install `build` in the Python environment that runs zdistro.
Isolation is enabled by default; `isolation = false` is available when the user
has already provisioned the backend. Metadata is read from the built archive,
not synthesized by zdistro. See [Python packaging flow](https://packaging.python.org/en/latest/flow/)
and [wheel format](https://packaging.python.org/en/latest/specifications/binary-distribution-format/).

Cargo source packages are made with `cargo package`, including its normal
verification build. The normalized `Cargo.toml` must match the unified package
name and version. Select a single package project for each target. See
[Cargo package](https://doc.rust-lang.org/cargo/commands/cargo-package.html).

npm packages use `npm pack --ignore-scripts`; explicit language builds can run
`npm run <script>` before native packaging. `build.install = true` runs `npm ci
--ignore-scripts` in the private project copy first. Source manifests and
lockfiles are preserved. See [npm pack](https://docs.npmjs.com/cli/v12/commands/npm-pack/).

## Browser extensions

`extension` creates a ZIP with `manifest.json` at its root. `xpi` uses the same
WebExtension layout with an XPI suffix. The validator checks manifest version,
name/version and declared local script/page/icon resources. Browser API
compatibility and store policy validation remain browser-specific; run that
browser's linter and runtime tests in your build recipe when required.

Chrome accepts an extension ZIP for upload. Firefox distribution normally
involves Mozilla signing. The built-in XPI output is unsigned. See
[Chrome preparation](https://developer.chrome.com/docs/webstore/prepare),
[WebExtension manifest](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json)
and [Mozilla extension signing](https://wiki.mozilla.org/Add-ons/Extension_Signing).

## macOS and Windows

PKG and DMG adapters run on macOS. PKG files use the declared package identifier
and install the staged relative paths under `/`. DMG files use a read-only UDZO
image. They are left unsigned for an explicit signing/notarization workflow.
Apple describes the distinction between containers, signing and distribution
in [Packaging Mac software](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution).

MSI packages are generated from staged files through WiX's native schema and
CLI. The installer uses a per-machine Program Files directory. Verification
decodes the MSI database; it does not perform an installation or the full ICE
suite. See [WiX CLI](https://docs.firegiant.com/wix/tools/wixexe/) and
[WiX validation](https://docs.firegiant.com/wix/tools/validation/).

MSIX requires a real staged Windows PE executable at `targets.msix.executable`.
The backend generates a desktop manifest, neutral template icons, block map and
package through MakeAppx. Specify the correct publisher and identity for later
signing. The generated manifest requests `runFullTrust` for a Win32 executable.
See [MakeAppx](https://learn.microsoft.com/en-us/windows/msix/package/create-app-package-with-makeappx-tool).

## Android

The APK template builds a small no-code Android package with AAPT2 and an
installed SDK platform. For an existing Android app, select `mode = "gradle"`,
point `project` at its source, and declare the task and relative output artifact.
The Gradle wrapper must be complete. SDK paths come from `ANDROID_HOME` or
`ANDROID_SDK_ROOT`; `platform = "android-35"` can choose an installed platform.
No SDK is downloaded or configured by zdistro.

AAB uses the existing Gradle project and validates its output with bundletool.
Provide `bundletool_jar` or the `BUNDLETOOL_JAR` environment variable. Missing
Gradle projects, tools and SDK components are reported by doctor. The checked-in
CI records an explicit AAB prerequisite gate. See [Android command-line builds](https://developer.android.com/build/building-cmdline),
[AAPT2](https://developer.android.com/tools/aapt2) and
[bundletool](https://developer.android.com/tools/bundletool).

## Containers

Docker uses an explicit Buildx `docker` or `oci` exporter to write an archive.
This works with builders that keep results only in their build cache unless an
output is requested. OCI export needs an OCI-capable Buildx driver. Podman builds
a unique local image, saves the selected archive format and removes only that
build's unique tag. No image is pushed to a registry by the backend.

Verification follows archive references and validates SHA-256/size for
recursively referenced OCI blobs. Docker image archives are checked for a valid
manifest, configuration and referenced layer files. See [Docker exporters](https://docs.docker.com/build/exporters/),
[Docker save](https://docs.docker.com/reference/cli/docker/image/save/) and the
[OCI image layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md).

## Extension contract

A custom backend declares hosts, tool names, literal build argv arrays, an output
path and literal verify argv arrays. Success requires both command groups and a
nonempty regular artifact inside the assigned output directory. Receipts label
these results `recipe`, keeping the supplied verifier's scope explicit. Add new
language tools through `build.kind = "command"`; JSON/TOML and compression stay
with standard libraries and ecosystem tools.

Staging supports regular files and rejects escaping paths, file collisions and
unsupported links. Source trees containing application-specific symlink layouts
can use an explicit native recipe. Build scripts remain executable project code;
this utility provides isolated working directories, not an operating-system
sandbox.
