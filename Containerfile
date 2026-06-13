FROM ghcr.io/gardenlinux/builder:98ee0d480844b2d041524841bfdbbb4007d32248@sha256:d7063f72c0db3e7cdd618136efb292379794a0c4d4b5ddfc3759795c17d963ab

RUN sed 's/version="$2"/version=\$(echo \$2 | cut -d. -f 1-2).0/' -i /builder/bootstrap
