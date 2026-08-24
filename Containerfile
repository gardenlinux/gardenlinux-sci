FROM ghcr.io/gardenlinux/builder:0196add0ce875ac0b7721c19b2ee3ac37cf84387@sha256:a25e60658d595f0040b516fedaa067802e69f40810ebdd95ade5a59385eabf4c

RUN sed 's/version="$2"/version=\$(echo \$2 | cut -d. -f 1-2)/' -i /builder/bootstrap
