FROM --platform=linux/amd64 public.ecr.aws/docker/library/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget ca-certificates gzip unzip curl && \
    rm -rf /var/lib/apt/lists/*

# Install the NCBI SRA toolkit. The toolkit binaries are dynamically wired to
# sibling files inside the extracted bin/ directory, so the whole bin/ must stay
# together and be placed on PATH. Moving a single binary out (the previous bug)
# breaks fasterq-dump with "No such file or directory".
RUN wget -q https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/current/sratoolkit.current-ubuntu64.tar.gz && \
    tar xzf sratoolkit.current-ubuntu64.tar.gz && \
    rm sratoolkit.current-ubuntu64.tar.gz && \
    mv sratoolkit.* /opt/sratoolkit
ENV PATH="/opt/sratoolkit/bin:${PATH}"

# Install AWS CLI v2 for the unsigned S3 download from the Open Data SRA mirror
# and the upload of FASTQ to the data lake.
RUN curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws

# fasterq-dump needs a writable NCBI config directory. It is created in the
# scratch mount at runtime; disable the interactive config wizard here.
ENV NCBI_SETTINGS=/tmp/fastq/.ncbi/user-settings.mkfg
ENV VDB_CONFIG=/tmp/fastq/.ncbi

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# Strip any CRLF line endings before making the script executable. A CRLF
# shebang makes the kernel look for an interpreter named "bash\r" and the task
# exits 127. .gitattributes enforces LF in the repo; this is defense in depth so
# the image is correct even if the file reaches the build with CRLF.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

# FR-019: Container security — non-root user.
RUN groupadd --system --gid 1000 amruser && \
    useradd --system --uid 1000 --gid amruser --home-dir /data --shell /sbin/nologin amruser && \
    mkdir -p /data && chown -R amruser:amruser /data

WORKDIR /data
USER 1000:1000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
