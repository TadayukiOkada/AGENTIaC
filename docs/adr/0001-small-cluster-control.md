# ADR 0001: SSH, Ansible, systemd and Docker

Status: accepted for baseline.

Three heterogeneous workers already have Docker, CUDA and SSH. Choose Ansible for deployment, systemd for persistent lifecycle and SSH forwards, Docker for inference isolation. Reject k3s/Kubernetes because its scheduler/device/network control is not needed here; reject replacing Docker with Podman or host OS with Nix for now. Expose only loopback APIs; no firewall edits. Use inventory and model profiles to extend.

Consequence: control remains the management point; container GPU runtime options are node-specific. Native source build in dedicated project paths is an initial reproducibility compromise justified by existing Jetson toolchain/driver ABI. Exact source/compiler/image metadata is mandatory; future containerized builds can improve isolation without host stack replacement.
