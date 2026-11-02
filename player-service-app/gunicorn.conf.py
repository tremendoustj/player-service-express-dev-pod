from pathlib import Path

from server_logging import LOGGING_CONFIG


chdir = str(Path(__file__).resolve().parent)
wsgi_app = "wsgi:app"
bind = "127.0.0.1:8000"
workers = 1
worker_class = "gthread"
threads = 4
preload_app = False
reload = False
control_socket_disable = True

timeout = 30
graceful_timeout = 30
keepalive = 2
limit_request_line = 8190
limit_request_fields = 32
limit_request_field_size = 4096
forwarded_allow_ips = ""
secure_scheme_headers = {}

accesslog = None
errorlog = "-"
loglevel = "info"
capture_output = False
syslog = False
logconfig_dict = LOGGING_CONFIG


def on_starting(server):
    if server.cfg.workers != 1 or server.cfg.worker_class_str != "gthread":
        raise RuntimeError(
            "Player Service requires exactly one gthread worker per scrape target"
        )
    if server.cfg.reload:
        raise RuntimeError("The production profile does not support code reloading")


def nworkers_changed(server, new_value, old_value):
    if new_value != 1:
        raise RuntimeError(
            "Player Service requires exactly one worker per scrape target"
        )
