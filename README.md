# navsync

Syncs inventory data from [NAV](https://nav.uninett.no/) into
[Netbox](https://netbox.dev/).

`navsync` discovers the NAV instances to read from by looking them up in Netbox
itself: any Device or VM with the role slug `verktykassecnaas`, status `Active`
and tag `navsync` is treated as a NAV server (see
[Netbox prerequisites](#netbox-prerequisites)). For each one it reads the NAV inventory over NAV's API and
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
environment using the `NAVSYNC_` prefix, e.g. `NAVSYNC_NETBOX__TOKEN` can be used to
configure the netbox token.

A minimal configuration:

```toml
[netbox]
# URL of the Netbox instance to sync into.
url = "http://127.0.0.1:8080"

# An API token with read/write access to the Netbox instance's 'DCIM', 'IPAM',
# 'Plugins (Inventory)', 'Virtualization', 'Tenancy' and 'Extras' API endpoints.
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

## Netbox prerequisites

The following must exist in Netbox before running `navsync`.

**Plugin**

- [netbox-inventory](https://github.com/ArnesSI/netbox-inventory), which is
  used for assets.

**Tags**

- `navsync`: set this on the NAV server devices and VMs you want to sync.
- `cnaas`: navsync puts it on everything it creates, and only cleans up
  objects that have both `navsync` and `cnaas`. If this tag does not exist, it
  is silently left off.

**Device roles**

These are matched by name. If a role is missing, the sync is aborted.

- `router`, `switch`, `PDU`, `server`, `Uninett Environmental`, `unknown`
- A role with slug `verktykassecnaas`, which marks NAV servers. It must be
  usable on both devices and VMs.

**Custom fields**

| Name               | Object types                 | Type                  |
| ------------------ | ---------------------------- | --------------------- |
| `nav_url`          | Site, Location, Device       | Text/URL              |
| `software_version` | Asset (inventory)            | Text                  |
| `owner`            | Virtual machine              | Object (Tenant)       |

**NAV servers**

Each NAV server needs:

- role `verktykassecnaas`, status `Active` and tag `navsync`
- a name that is the hostname NAV is reachable on
- a tenant
- an owner. On a VM this is the `owner` custom field. On a device it is the
  owning tenant of its assigned inventory asset.

**Tenants (optional)**

NAV organizations under `cnaas` are mapped to the Netbox tenant with the same
name as the organization ID. If no tenant matches, the NAV server's tenant is
used.

Manufacturers, device types, sites, locations, interfaces, IP addresses,
prefixes and virtual chassis are all created automatically.

## NAV prerequisites

**Organizations**

- Only netboxes owned by the organization `cnaas` or one of its descendants
  are synced. All others are skipped.
- A netbox owned by `cnaas` itself gets the NAV server's Netbox tenant. A
  netbox owned by a descendant organization gets the Netbox tenant with the
  same name as that organization's ID, falling back to the NAV server's
  tenant.

**Locations and rooms**

- The Netbox site is taken from the room's location tree, walking up from the
  room. It is the nearest location with an address (the `addr` field in the
  location's data), or the top-level location if none has an address.
- The locations between the room and the site become nested Netbox locations,
  and the room becomes the innermost Netbox location.
- Site names must be unique across all NAV servers. A duplicate aborts the
  sync.
- The address is geocoded with Kartverket, so it must be a Norwegian address
  to get coordinates.

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

This repository uses [pre-commit](https://pre-commit.com/) for linting and
formatting:

```shell
pip install pre-commit
pre-commit install
```

## License

Apache-2.0
