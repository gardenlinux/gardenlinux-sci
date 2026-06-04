FROM ghcr.io/gardenlinux/builder:98ee0d480844b2d041524841bfdbbb4007d32248

RUN sed 's/version="$2"/version=\$(echo \$2 | cut -d. -f 1-2).0/' -i /builder/bootstrap
