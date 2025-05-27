import os

from dynaconf import Dynaconf, Validator
from platformdirs import user_config_dir

settings = Dynaconf(
    settings_files=[
        "netbox-tools.toml",
        os.path.join(user_config_dir("netbox-tools"), "netbox-tools.toml"),
    ],
    envvar_prefix="NETBOX_TOOLS",
    validators=[
        Validator("kind.database_url", must_exist=True, startswith="postgresql://")
    ],
)
