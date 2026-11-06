#!/bin/sh
# make-ca: write a throwaway fixture CA to /fixture/ca (ca.pem, ca-key.pem).
# serve:   run Squid with /fixture/squid.conf, signing with that CA.
set -eu
case "${1:-serve}" in
  make-ca)
    mkdir -p /fixture/ca
    cd /fixture/ca
    openssl ecparam -name prime256v1 -genkey -noout -out ca-key.pem
    openssl req -new -x509 -key ca-key.pem -out ca.pem -days 2 \
      -subj "/CN=project-sandbox copilot-egress e2e fixture CA" \
      -addext "basicConstraints=critical,CA:true,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign"
    chmod 0644 ca.pem
    chmod 0600 ca-key.pem
    ;;
  serve)
    mkdir -p /run/fixture-ca
    cp /fixture/ca/ca.pem /fixture/ca/ca-key.pem /run/fixture-ca/
    /usr/lib/squid/security_file_certgen -c -s /run/fixture-ca/ssl_db -M 4MB >/dev/null
    cp /fixture/squid.conf /etc/squid/squid.conf
    chown -R squid:squid /run/fixture-ca /etc/squid/squid.conf
    chmod 0600 /run/fixture-ca/ca-key.pem
    # Squid cannot reopen a root-owned console fd after dropping privileges
    # (spike 0.2), so it runs as squid from the start, logging into a pipe.
    exec su-exec squid sh -c 'squid -N -f /etc/squid/squid.conf 2>&1 | cat'
    ;;
  *)
    echo "usage: fixture-entrypoint make-ca|serve" >&2
    exit 64
    ;;
esac
