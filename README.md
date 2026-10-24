# Workstation Provisioning

## Introduction

This is a set of Ansible scripts to automate:

- Installation of Podman
- Restrictive `ufw` firewall configuration. Based on [DigitalOcean's UFW instructions](https://www.digitalocean.com/community/tutorials/how-to-setup-a-firewall-with-ufw-on-an-ubuntu-and-debian-cloud-server).
- Installation of NVIDIA GPU support. Based on instructions for installing the [drivers](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/index.html) and [container toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html#installing-with-apt).
- Removal of insecure CUPS
- Disabling swap

## Prerequisites

```shell
sudo apt-get -yq update
sudo apt-get -yq install ansible-core
install -m 0700 -d $HOME/.ansible/tmp
```

## Installation

```shell
sudo ansible-playbook -i "localhost," --connection=local playbook.yaml
```

## The `fake-interview-malware-research` Skill

Caution: Put it at user level so it runs from the home directory, never from inside the repository it's checking - that way the repository's `.claude/`, `.mcp.json` and `CLAUDE.md` never get loaded.

## To Do

- [How to secure anything](https://github.com/veeral-patel/how-to-secure-anything)
- KeePassXC in air-gapped and encrypted VM
