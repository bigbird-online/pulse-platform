FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436

ARG PI_VERSION=0.87.1
ARG PI_COMMIT=f07218c4d4bbc12bef056a7058c3dd49dfe41abe
ARG PI_TARBALL_SHA512=9bc02b254b557103127b594b13f122eef5ff255ecedfdb1699606c5d5d8d394ef4174a82a7c1ae6c0db8a53dcb9f04e633a2cbdb157cd3f87ab1083ce67ebd7b

LABEL org.opencontainers.image.source="https://github.com/earendil-works/pi"
LABEL org.opencontainers.image.version="${PI_VERSION}"
LABEL org.opencontainers.image.revision="${PI_COMMIT}"

RUN set -eux; \
    test "$(npm view "@earendil-works/pi-coding-agent@${PI_VERSION}" gitHead)" = "${PI_COMMIT}"; \
    npm pack "@earendil-works/pi-coding-agent@${PI_VERSION}" --pack-destination /tmp; \
    tarball="/tmp/earendil-works-pi-coding-agent-${PI_VERSION}.tgz"; \
    printf '%s  %s\n' "${PI_TARBALL_SHA512}" "${tarball}" | sha512sum -c -; \
    npm install --global --omit=dev "${tarball}"; \
    rm "${tarball}"; \
    pi --version | grep -E "(^|[^0-9])${PI_VERSION}([^0-9]|$)"

USER 65532:65532
WORKDIR /workspace
CMD ["/usr/local/bin/pi", "--help"]
