import os

# The settings are validated the first time they are read, so make sure the
# required ones exist even when there is no navsync.toml, e.g. in CI.
os.environ.setdefault("NAVSYNC_NETBOX__URL", "http://netbox.test")
os.environ.setdefault("NAVSYNC_NETBOX__TOKEN", "test-token")
