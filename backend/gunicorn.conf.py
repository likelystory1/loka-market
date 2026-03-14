# Gunicorn production configuration
# Start with: gunicorn -c gunicorn.conf.py server:app

# Expose on all interfaces — put Nginx/firewall in front if needed
bind = "0.0.0.0:5000"

# 1 worker + threads: background threads (scraper, poller) only run once
# If you add Nginx + multiple workers, move scraper to a separate systemd service
workers = 1
worker_class = "gthread"
threads = 8

# Kill requests taking too long
timeout = 60
keepalive = 5

# Log to stdout/stderr (captured by systemd or screen)
accesslog = "-"
errorlog  = "-"
loglevel  = "info"

forwarded_allow_ips = "*"
