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
        Validator("kind.database_url", must_exist=True, startswith="postgresql://"),
        Validator(
            "netbox.url",
            must_exist=True,
            condition=lambda v: v.startswith("http://") or v.startswith("https://"),
        ),
        Validator("netbox.token", must_exist=True),
    ],
)
