FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 build-essential rustc cargo ca-certificates && dpkg-query -W > /packages.manifest && rm -rf /var/lib/apt/lists/*
WORKDIR /work
