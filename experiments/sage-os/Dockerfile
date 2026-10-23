FROM rust:1.99.1-slim-bookworm@sha256:a24e292b4af7bdee351a31d3a6379972a5652f0ea22a1b6d6883324ac4f5097c AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY core core
COPY sage sage
COPY wasm wasm
COPY routines routines
RUN cargo build --release --locked -p sage -p sage-routines && mkdir /data

# No shell, no package manager: one binary and its home.
FROM gcr.io/distroless/cc-debian12:nonroot@sha256:9dac0a79194e45a7da0158a9c6da57b217585af0786db3845d1f0ec1a0dd182f
COPY --from=build /src/target/release/sage /sage
# The routine runner: the same image, run as its own sealed service (compose.yaml).
COPY --from=build /src/target/release/sage-routines /sage-routines
COPY --from=build --chown=65532:65532 /data /data
ENV SAGE_HOME=/data SAGE_BIND=0.0.0.0:8766
ENTRYPOINT ["/sage"]
