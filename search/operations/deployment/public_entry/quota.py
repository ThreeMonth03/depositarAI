"""Render native APISIX counters; the adapter supplies validated request variables."""

from pathlib import Path


def quota_response_lua():
    return Path(__file__).with_name("quota_response.lua").read_text(encoding="utf-8")


def render_counter(config, *, session=False):
    period = "session" if session else "quota"
    suffix = f"$http_x_depositar_quota_bucket:$http_x_depositar_{period}_period"
    window = f"$http_x_depositar_{period}_ttl"
    rules = [
        {
            "count": config.session_limit
            if session
            else "$http_x_depositar_turn_limit",
            "time_window": window,
            "key": "individual:$http_x_depositar_principal_kind:$http_x_depositar_subject:"
            + suffix,
            "header_prefix": "Individual",
        }
    ]
    if not session and config.access_mode == "mixed":
        rules.append(
            {
                "count": "$http_x_depositar_quota_pool_limit",
                "time_window": window,
                "key": "pool:$http_x_depositar_principal_kind:" + suffix,
                "header_prefix": "Pool",
            }
        )
    rules.append(
        {
            "count": 100 if session else config.quota_global,
            "time_window": window,
            "key": "global:" + suffix,
            "header_prefix": "Global",
        }
    )
    return {
        "policy": "redis",
        "redis_host": "quota-redis",
        "redis_port": 6379,
        "redis_username": "gateway",
        "redis_password": "$ENV://DEPOSITAR_REDIS_PASSWORD",
        "redis_database": 0,
        "redis_timeout": 1000,
        "allow_degradation": False,
        "sync_interval": -1,
        "window_type": "fixed",
        "rejected_code": 429,
        "rejected_msg": "quota_exceeded",
        "show_limit_quota_header": True,
        # One common turn route gives both tiers one native global namespace.
        # Rules count attempted admissions sequentially; they are not a transaction.
        "rules": rules,
        "_meta": {"error_response": {"error": {"code": "quota_exceeded"}}},
    }
