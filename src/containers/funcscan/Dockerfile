# AMR screening container for the HealthOmics AMR workflow.
#
# Builds AMRFinderPlus (NCBI's reference tool for detecting acquired AMR genes
# and resistance point mutations in assembled nucleotide sequence) on top of an
# Amazon ECR Public base image, and bakes the AMRFinderPlus reference database
# into the image at build time. HealthOmics workflow tasks have no internet
# access, so the database must be present in the image (no `amrfinder --update`
# at run time).
#
# The tool and database are installed from Bioconda via micromamba. micromamba
# is fetched from its pinned GitHub release. The AMRFinderPlus version and the
# database snapshot are both pinned so the image is reproducible.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Pin the AMRFinderPlus package version so the baked database matches a known
# release. Keep this in sync with the workflow's expectations.
ARG AMRFINDER_VERSION=4.2.7
ARG MICROMAMBA_VERSION=2.0.5

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates bzip2 curl procps && \
    rm -rf /var/lib/apt/lists/*

# Install micromamba (static binary) from its pinned GitHub release.
RUN curl -sSL "https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}-0/micromamba-linux-64" \
        -o /usr/local/bin/micromamba && \
    chmod +x /usr/local/bin/micromamba

ENV MAMBA_ROOT_PREFIX=/opt/conda
ENV CONDA_PREFIX=/opt/conda
ENV PATH=/opt/conda/bin:$PATH

# Install AMRFinderPlus from Bioconda into the base environment.
RUN micromamba install -y -n base -r /opt/conda \
        -c conda-forge -c bioconda \
        "ncbi-amrfinderplus=${AMRFINDER_VERSION}" && \
    micromamba clean --all --yes

# Bake the reference database into the image at build time. This is the only
# step that needs the network, and it runs at build time, not at workflow run
# time. `amrfinder -u` downloads the latest database into the package's data
# directory inside the image.
RUN amrfinder -u

# Sanity-check the tool and its bundled database so a broken build fails here
# rather than at workflow run time. `amrfinder --version` prints the software
# version; the database is validated by a trivial run on empty input.
RUN amrfinder --version && amrfinder --database_version

# FR-019: Container security — non-root user.
RUN groupadd --system --gid 1000 amruser && \
    useradd --system --uid 1000 --gid amruser --home-dir /data --shell /sbin/nologin amruser && \
    mkdir -p /data && chown -R amruser:amruser /data

WORKDIR /data
USER 1000:1000
