# navsync

Syncs inventory data from [NAV](https://nav.uninett.no/) into
[Netbox](https://netbox.dev/).

`navsync` discovers the NAV instances to read from by looking them up in Netbox
itself: any Device or VM with role `Verktøykasse` and status `Active` is treated
as a NAV server. For each one it reads the NAV inventory over NAV's API and
creates or updates the corresponding sites, locations, racks, devices,
interfaces, IP addresses and prefixes in Netbox.

## Installation

It is recommended to install into a Python virtual environment:

```shell
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

To also install development dependencies (required for running tests):

```shell
pip install ".[dev]"
```

## Configuration

`navsync` needs credentials for both Netbox and the NAV instances it syncs from.

Configuration is read from `navsync.toml`, which is looked for in the current
working directory, in any of its parent directories, and in the system's
standard user configuration directory — e.g.
`~/.config/navsync/navsync.toml`. Settings can also be supplied through the
environment using the `NAVSYNC_` prefix.

A minimal configuration:

```toml
[netbox]
# URL of the Netbox instance to sync into.
url = "http://127.0.0.1:8080"

# An API token with read/write access to the Netbox instance's 'DCIM', 'IPAM',
# 'Plugins (Inventory)', 'Virtualization' and 'Tenancy' API endpoints.
token = "0123456789"

[nav]
# Path to a PEM-encoded RSA private key used to sign the JWTs that authenticate
# navsync towards the NAV API. The corresponding public key must be registered
# with each NAV instance to be accessed.
private_key_path = "/path/to/private_key.pem"

# How long (in seconds) a generated JWT should be valid for.
expiry_delta = 3600

# Sets the 'iss' claim of generated tokens. Must match the value configured in
# the NAV instances being synced against.
issuer = "navsync"

# If true, calls to the NAV APIs use https://, otherwise http://.
https = true
```

## Usage

```shell
navsync
```

Useful flags:

- `--nosync` — fetch data from the NAV instances but make no changes in Netbox.
  Useful for testing connectivity and permissions.
- `--loglevel {DEBUG,INFO,WARNING,ERROR,CRITICAL}` — defaults to `WARNING`.

## Running tests

```shell
pytest
```

Run a specific test file, class or test:

```shell
pytest tests/test_location_hierarchy_parser.py
pytest tests/test_location_hierarchy_parser.py::TestSimpleHierarchy
pytest tests/test_location_hierarchy_parser.py::TestSimpleHierarchy::test_root_nav_location_should_become_a_site
```

## Development

See [docs/development.md](docs/development.md) for how to set up local NAV and
Netbox instances to test against, and for notes on how NAV's data model is
mapped onto Netbox's.

This repository uses [pre-commit](https://pre-commit.com/) for linting and
formatting:

```shell
pip install pre-commit
pre-commit install
```

## License

Apache-2.0
