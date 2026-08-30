ARG SOURCE_DATE_EPOCH=1782770622
FROM ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

ENV DEBIAN_FRONTEND=noninteractive
COPY apt-sources.list /etc/apt/sources.list
RUN echo "1a4dd63e5c76728960a2edddae22e2e0fc53df8e8b87806deb971030ac704eb0  /usr/share/keyrings/ubuntu-archive-keyring.gpg" | sha256sum -c - && \
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
