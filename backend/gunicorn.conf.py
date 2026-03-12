# Gunicorn production configuration
# Start with: gunicorn -c gunicorn.conf.py server:app

import multiprocessing

# Bind to localhost only — Nginx handles public traffic
bind = "127.0.0.1:5000"

# 2–4 workers for a small VPS (1–2 vCPU); scale up if needed
workers = multiprocessing.cpu_count() * 2 + 1

# Use gevent or gthread if UUID lookups cause blocking; sync is fine for low traffic
worker_class = "sync"

# Prevent runaway requests
timeout = 30
keepalive = 5

# Log to stdout/stderr (captured by systemd)
accesslog = "-"
errorlog  = "-"
loglevel  = "info"

# Security: don't leak server version
forwarded_allow_ips = "127.0.0.1"
