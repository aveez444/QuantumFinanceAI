# settings.py - Multi-tenant ERP Configuration

from pathlib import Path
import os
from dotenv import load_dotenv
from datetime import timedelta
from decouple import config
from celery.schedules import crontab

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Security
# Security
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-change-this-in-production')

# Make DEBUG environment-driven so production can disable it safely.
# Accepts "1", "true", "yes" (case-insensitive) as truthy values.
DEBUG = os.getenv("DEBUG", "False").lower() in ("1", "true", "yes")


ALLOWED_HOSTS = ["quantumfinanceai.onrender.com"]


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'core',
    'analytics',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',   # <-- add this
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',    
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.middleware.TenantMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'organization.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'organization.wsgi.application'

import dj_database_url

# Database Configuration
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/erp_multitenant"),
        conn_max_age=600,
        ssl_require=True
    ),
    "analytics": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("ANALYTICS_DB_NAME", "erp_analytics"),
        "USER": os.getenv("DB_USER", "postgres"),
        "PASSWORD": os.getenv("DB_PASSWORD", "1"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    },
}


AI_SETTINGS = {
    'GROQ_API_KEY': os.getenv('GROQ_API_KEY', ''),
    'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY', ''),
    'DEFAULT_MODEL': 'llama-3.1-8b-instant',
    'MAX_TOKENS': 512,
    'TEMPERATURE': 0.3,
}

# For correct behaviour behind proxies
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "x-tenant-id",   # ✅ Add this line
]

# Remove session configuration since we're using JWT
# CORS Configuration - Update for JWT
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    'http://172.20.112.1:5173',
    'https://quantumfinanceai.onrender.com',
    "https://quantum-ai-frontend.vercel.app",
]
CORS_ALLOW_CREDENTIALS = False  # Set to False for JWT
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    'http://172.20.112.1:5173',
    'https://quantumfinanceai.onrender.com',
    "https://quantum-ai-frontend.vercel.app",  # <-- add this
]

# Rest Framework - Update for JWT
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

# JWT Settings
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': False,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': False,

    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': None,
    'AUDIENCE': None,
    'ISSUER': None,

    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',

    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',

    'JTI_CLAIM': 'jti',

    'SLIDING_TOKEN_REFRESH_EXP_CLAIM': 'refresh_exp',
    'SLIDING_TOKEN_LIFETIME': timedelta(minutes=5),
    'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=1),
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"

# Where `manage.py collectstatic` will collect static files to
STATIC_ROOT = BASE_DIR / "staticfiles"

# Where you keep your project's source static assets during development.
# Create this folder if it doesn't exist, or remove/adjust this line.
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30  # 30 days
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Multi-tenant settings
TENANT_SETTINGS = {
    'DEFAULT_SUBDOMAIN': 'app',
    'TENANT_CACHE_TTL': 3600,
    'ENABLE_SUBDOMAIN_ROUTING': True,
}

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs' / 'erp.log',
            'formatter': 'verbose',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'core': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

os.makedirs(BASE_DIR / 'logs', exist_ok=True)


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CELERY_BEAT_SCHEDULE = {
    'send-weekly-reports': {
        'task': 'core.tasks.send_weekly_reports',
        'schedule': crontab(day_of_week='sunday', hour=8, minute=0),
    },
}

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'foodoindia@gmail.com'
EMAIL_HOST_PASSWORD = 'ewck ezsv sdpj thdk'
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = 'noreply@yourerp.com'

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = config('AWS_ACCESS_KEY_ID', default='')
AWS_SECRET_ACCESS_KEY = config('AWS_SECRET_ACCESS_KEY', default='')
AWS_STORAGE_BUCKET_NAME = config('AWS_STORAGE_BUCKET_NAME', default='your-erp-bucket')
AWS_S3_REGION_NAME = config('AWS_S3_REGION_NAME', default='us-east-1')

# S3 Settings
AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'
AWS_DEFAULT_ACL = None
AWS_S3_OBJECT_PARAMETERS = {
    'CacheControl': 'max-age=86400',  # 1 day cache
}

# Security Settings
AWS_S3_FILE_OVERWRITE = False  # Don't overwrite files with same name
AWS_S3_SECURE_URLS = True
AWS_LOCATION = 'media'  # Folder in S3 bucket

# File Upload Settings
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
STATICFILES_STORAGE = 'storages.backends.s3boto3.S3StaticStorage'  # Optional: for static files too

# File Size Limits (in bytes)
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10MB in memory
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024   # 50MB total

# Allowed file types by category
ALLOWED_DOCUMENT_EXTENSIONS = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'csv']
ALLOWED_IMAGE_EXTENSIONS = ['jpg', 'jpeg', 'png', 'gif', 'bmp']
ALLOWED_CAD_EXTENSIONS = ['dwg', 'dxf', 'step', 'iges', 'stl']
ALLOWED_ARCHIVE_EXTENSIONS = ['zip', 'rar', '7z']

# File storage paths
MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/media/'
MEDIA_ROOT = '/media/'  # Not used with S3 but required

# Alternative: Local development settings
if config('USE_S3', default=True, cast=bool) is False:
    DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
    MEDIA_URL = '/media/'
    MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Celery for background file processing (optional)
CELERY_BROKER_URL = config('REDIS_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = config('REDIS_URL', default='redis://localhost:6379/0')
CELERY_BEAT_SCHEDULE = {
    'cleanup-old-gl-journals': {
        'task': 'your_app.tasks.cleanup_old_gl_journals',
        'schedule': crontab(day_of_month=1, hour=2, minute=0),  # 1st of month, 2 AM
    },
}

# File processing settings
MAX_FILE_SIZE = {
    'document': 25 * 1024 * 1024,    # 25MB for documents
    'image': 10 * 1024 * 1024,       # 10MB for images
    'cad_drawing': 100 * 1024 * 1024, # 100MB for CAD files
    'archive': 50 * 1024 * 1024,     # 50MB for archives
}