#!/bin/sh
set -eu

runtime_config=/tmp/sandbox-console.conf
runtime_main_config=/tmp/sandbox-console-nginx.conf
dns_server="$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf)"
[ -n "$dns_server" ] || dns_server=127.0.0.1
case "$dns_server" in
    *:*) dns_server="[${dns_server}]" ;;
esac

sed "s/__SANDBOX_RESOLVER__/${dns_server}/" \
  /etc/nginx/templates/default.conf.template >"$runtime_config"

cat >"$runtime_main_config" <<'EOF'
worker_processes auto;
error_log /dev/stderr notice;
pid /tmp/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';
    access_log /dev/stdout main;

    proxy_temp_path /tmp/proxy_temp;
    client_body_temp_path /tmp/client_temp;
    fastcgi_temp_path /tmp/fastcgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;
    scgi_temp_path /tmp/scgi_temp;

    sendfile on;
    keepalive_timeout 65;
    include /tmp/sandbox-console.conf;
}
EOF

exec "$@"
