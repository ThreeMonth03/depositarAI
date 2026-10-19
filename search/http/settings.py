"""Configure the stateless JSON HTTP applications."""

DEPOSITAR_HTTP = True
ROOT_URLCONF = "search.http.application"
MIDDLEWARE = ["search.http.application.ApplicationMiddleware"]
INSTALLED_APPS = []
ALLOWED_HOSTS = ["*"]
APPEND_SLASH = False
DEBUG = False
USE_I18N = False
USE_TZ = True
FILE_UPLOAD_MAX_MEMORY_SIZE = 8192
DATA_UPLOAD_MAX_MEMORY_SIZE = 8192
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "UNAUTHENTICATED_USER": None,
}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"django": {"handlers": ["null"], "propagate": False}},
}
