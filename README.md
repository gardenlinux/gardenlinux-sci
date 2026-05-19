# Garden Linux CCloud flavors

This repository extends [Garden Linux](https://github.com/gardenlinux/gardenlinux) with custom flavors and features. It uses Garden Linux as a submodule and overrides specific features and flavors while maintaining compatibility with the original build system.

## Overview

This repository contains:

- Custom features in `features/`
- Custom flavor definitions in `flavors.yaml`
- CI/CD workflows for building and publishing images

## Make Targets

The following make targets are available:

- `prepare`: Initialize and update submodules, required for first-time setup
- `update [COMMIT=<hash>]`: Update Garden Linux submodule to latest (or specific) commit and sync workflow references
- `clean`: Remove Garden Linux submodule and reset the environment

<p align="center">
  <img alt="Bundesministerium für Wirtschaft und Energie (BMWE)-EU funding logo" src="https://apeirora.eu/assets/img/BMWK-EU.png" width="400"/>
</p>
