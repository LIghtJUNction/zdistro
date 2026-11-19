# zdistro

One manifest for language builds, file staging and native package formats.

The first iteration is being built around three boundaries: a language builder
produces files, a staging map chooses package paths, and a format backend invokes
the appropriate packaging tool. Native tools and SDKs remain prerequisites.
Successful builds will carry verification receipts rather than inferred support
claims. This repository is part of [Project Z](https://github.com/LIghtJUNction/project-z).

The initial source checkpoint contains the manifest and staging contracts.
Native backends, command-line workflows and measured support evidence follow
in separate commits.

Requires Python 3.11 or newer. Core runtime dependencies: Python standard library.

```sh
python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
