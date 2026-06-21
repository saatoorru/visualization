from flask_appbuilder.security.manager import AUTH_DB

SECRET_KEY = "super_secret_key_change_in_prod"
AUTH_TYPE = AUTH_DB
WTF_CSRF_ENABLED = False

FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
    "GLOBAL_ASYNC_QUERIES": False,
}

RESULTS_BACKEND = None

CACHE_CONFIG = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
}
