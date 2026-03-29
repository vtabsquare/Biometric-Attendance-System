import os

# Gunicorn configuration for Digital Ocean production deployment

# Bind to localhost only (nginx will proxy)
bind = f"127.0.0.1:{os.environ.get('PORT', '8080')}"

# Workers - recommended: (2 x CPU cores) + 1
workers = int(os.environ.get('GUNICORN_WORKERS', '3'))

# Worker class - sync is fine for this app
worker_class = 'sync'

# Timeout - face recognition can take time
timeout = 120

# Keep-alive
keepalive = 5

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Process naming
proc_name = 'faceauth'

# Preload app for memory efficiency
preload_app = True

# Graceful timeout
graceful_timeout = 30
