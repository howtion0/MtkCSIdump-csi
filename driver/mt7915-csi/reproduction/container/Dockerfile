FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        binutils \
        bison \
        build-essential \
        ca-certificates \
        clang \
        device-tree-compiler \
        dwarves \
        file \
        flex \
        gawk \
        gettext \
        git \
        libelf-dev \
        libncurses5-dev \
        libssl-dev \
        llvm \
        patch \
        python3 \
        python3-distutils \
        python3-pyelftools \
        python3-setuptools \
        rsync \
        squashfs-tools \
        swig \
        u-boot-tools \
        unzip \
        wget \
        zlib1g-dev \
        zstd \
    && rm -rf /var/lib/apt/lists/*
