# pgAdmin components for Arcnix

This branch builds the pgAdmin component that [Arcnix](https://github.com/arcnix-org/arcnix)
downloads the first time someone opens pgAdmin. It contains no changes to pgAdmin.
The fork's other branches mirror [pgadmin-org/pgadmin4](https://github.com/pgadmin-org/pgadmin4)
and are only used to propose fixes to pgAdmin.

A component is the official pgAdmin wheel from PyPI installed into a portable
Python ([python-build-standalone](https://github.com/astral-sh/python-build-standalone)),
packed as a zip with a `component.json` manifest. Components are built for
macOS on Apple Silicon (`macos-arm64`) and Windows (`windows-x64`). pgAdmin's
regression tests and test data are left out: they aren't used at runtime and
hold the longest paths, which Windows can't open without long-path support. Both downloads are checked
against SHA-256 digests: PyPI's for the wheel, `pins.json` for Python.

## Publishing a build

For a new pgAdmin version, first pin its dependencies:

```sh
python3 -I build.py --pgadmin 9.20 --build 1 --platform macos-arm64 --freeze
```

Review `constraints/9.20.txt` and commit it. Builds refuse a version without
one, so every component of a release uses the same dependency versions.

Then run **Actions → Build pgAdmin component** with:

- **pgadmin**: the version on PyPI, such as `9.19`.
- **build**: `1` for a new pgAdmin version. Increase it to rebuild the same
  version, for example after a Python security update in `pins.json`.
- **build_frontend**: leave off. The build refuses a wheel without pgAdmin's
  compiled interface. Turn this on only when pgAdmin has published such a
  wheel (9.19 did) and waiting for a fixed one is not an option. The interface
  is then built once from pgAdmin's `REL-x_y` tag with Node.js, and every
  platform uses that build.

Each platform's component is checked before the release: every setting
Arcnix overrides must still exist in pgAdmin, `setup.py setup-db` and
`load-servers` must save a server where Arcnix's launcher updates it, and the
backend must start, refuse requests without its key and serve its interface.
Then the release
`pgadmin-<version>-<build>` is published with the zips and `SHA256SUMS`.
Arcnix pins each zip's URL, size and SHA-256 in `pgadmin.rs`, so a release is
used only after an Arcnix change that names it.

## Building locally

```sh
python3 -I build.py --pgadmin 9.19 --build 1 --platform macos-arm64
```

Needs Python 3.9 or later and network access to PyPI and GitHub.

## Licences

pgAdmin is released under the PostgreSQL licence, and each component includes
pgAdmin's licence along with those of its dependencies. The build files in
this branch are under the Apache License 2.0, like Arcnix.
