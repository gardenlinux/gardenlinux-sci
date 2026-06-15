FROM ghcr.io/gardenlinux/builder:d6d24ba1aec66889a2acab83aedcb00e869abfcd@sha256:3dc78daebb56605baf105d2f20a6e8b94137237c1c2587b80d571fbb5c9f49ab

RUN sed 's/version="$2"/version=\$(echo \$2 | cut -d. -f 1-2)/' -i /builder/bootstrap
