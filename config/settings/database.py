import os

import dj_database_url


def database_config_from_env(env=None):
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL", "").strip()
    if database_url:
        database = dj_database_url.parse(
            database_url,
            conn_max_age=int(env.get("DATABASE_CONN_MAX_AGE", "0")),
            conn_health_checks=True,
            ssl_require=True,
        )
    else:
        database = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env.get("POSTGRES_DB", "ump_cferi"),
            "USER": env.get("POSTGRES_USER", ""),
            "PASSWORD": env.get("POSTGRES_PASSWORD", ""),
            "HOST": env.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": env.get("POSTGRES_PORT", "5432"),
            "OPTIONS": {
                "connect_timeout": int(env.get("POSTGRES_CONNECT_TIMEOUT", "5")),
            },
            "CONN_MAX_AGE": int(env.get("POSTGRES_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }

    return {"default": database}
