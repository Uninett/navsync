import os

from dynaconf import Dynaconf, Validator
from platformdirs import user_config_dir

settings = Dynaconf(
    settings_files=[
        "navsync.toml",
        os.path.join(user_config_dir("navsync"), "navsync.toml"),
    ],
    envvar_prefix="NAVSYNC",
    validators=[
        Validator(
            "netbox.url",
            must_exist=True,
            condition=lambda v: v.startswith("http://") or v.startswith("https://"),
        ),
        Validator("netbox.token", must_exist=True),
        Validator("nav.private_key_path", must_exist=True),
        Validator("nav.expiry_delta", must_exist=True),
        Validator("nav.issuer", must_exist=True),
    ],
    validate_only="netbox",
)
