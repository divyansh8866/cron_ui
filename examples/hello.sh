#!/bin/sh
# Example job script. Paste this into the "Script" box when creating a job,
# or adapt it. It runs as root inside the container.
echo "Hello from cron-ui at $(date)"
echo "Running as uid=$(id -u) gid=$(id -g)"

# Example: act on a mounted host path (mount it in docker-compose.yml first)
# ls -la /DATA

# Example: control another container via the mounted docker.sock
# docker restart some-container
