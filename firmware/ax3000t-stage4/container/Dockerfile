ARG SOURCE_DATE_EPOCH=1782770622
FROM ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

ENV DEBIAN_FRONTEND=noninteractive
COPY apt-sources.list /etc/apt/sources.list
ADD --checksum=sha256:6e8cdcc8c86103acd4fc14649eac62ff2037108389074a7b167567af33c32245 \
    https://snapshot.ubuntu.com/ubuntu/20260830T000000Z/pool/main/c/ca-certificates/ca-certificates_20260601%7e22.04.1_all.deb \
    /tmp/ca-certificates-bootstrap.deb
RUN echo "1a4dd63e5c76728960a2edddae22e2e0fc53df8e8b87806deb971030ac704eb0  /usr/share/keyrings/ubuntu-archive-keyring.gpg" | sha256sum -c - && \
    echo "6e8cdcc8c86103acd4fc14649eac62ff2037108389074a7b167567af33c32245  /tmp/ca-certificates-bootstrap.deb" | sha256sum -c - && \
    echo "115de07e0a83d82ccc2e3369ec8d4dd59041f7ea61c9d31b59bd7b419616c264f26843b6ed48de64e99d40846c0c55a311acd9bc8a403f39750ed9a0ac032d03  /tmp/ca-certificates-bootstrap.deb" | sha512sum -c - && \
    test "$(wc -c < /tmp/ca-certificates-bootstrap.deb)" -eq 140666 && \
    dpkg-deb -x /tmp/ca-certificates-bootstrap.deb /tmp/ca-certificates-bootstrap && \
    test "$(find /tmp/ca-certificates-bootstrap/usr/share/ca-certificates/mozilla -type f -name '*.crt' | wc -l)" -eq 121 && \
    mkdir -p /etc/ssl/certs && \
    find /tmp/ca-certificates-bootstrap/usr/share/ca-certificates/mozilla \
      -type f -name '*.crt' -print0 | LC_ALL=C sort -z | xargs -0 cat \
      > /etc/ssl/certs/ca-certificates.crt && \
    echo "9481fcd95f41b221f02f14d896535fe500bec539bc563c4cdca1acee483a8bdd  /etc/ssl/certs/ca-certificates.crt" | sha256sum -c - && \
    rm -rf /tmp/ca-certificates-bootstrap /tmp/ca-certificates-bootstrap.deb && \
    apt-get -o Acquire::Languages=none update && \
    apt-get install -y --no-install-recommends \
    bash build-essential clang flex bison g++ gawk gcc-multilib g++-multilib \
    gettext git libncurses-dev libssl-dev python3 python3-setuptools rsync \
    swig unzip zlib1g-dev file wget libelf-dev dwarves python3-pyelftools \
    python3-dev qemu-utils ccache rename libgmp3-dev libmpc-dev libfuse-dev \
    device-tree-compiler squashfs-tools zstd ca-certificates time jq patch \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.created="2026-06-29T22:03:42Z" \
      org.opencontainers.image.title="AX3000T Stage4 reproducible builder" \
      org.opencontainers.image.source="https://github.com/howtion0/MtkCSIdump-csi"

WORKDIR /stage4
ENTRYPOINT ["/bin/bash"]
