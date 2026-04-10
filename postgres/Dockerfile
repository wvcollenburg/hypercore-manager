FROM postgres:16

# Bake init scripts directly into the image so they are always present
# in /docker-entrypoint-initdb.d/ regardless of host OS or mount behaviour.
# Scripts run in filename order on a fresh (empty) data volume.
COPY init/*.sql /docker-entrypoint-initdb.d/
